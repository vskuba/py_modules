"""Агенты DeepSeek Harness как субагенты: сессии, задачи, ответы, доска промптов.

Внешний агент на той же машине, что и хост (Claude Code в роли оркестратора), раздаёт
работу агентам Harness так же, как человек в браузере: заводит карточку под проект,
шлёт промпт, ждёт ответ. Повторяющуюся работу кладёт на доску плагина
`dsh-continuous-agent` («ИИ-agent» в шапке карточки) — там она идёт очередью и по
расписанию сама.

Две двери, обе чужие:

* **Сессии** — штатный `/api` хоста (`dsh_session`).
* **Доска** — маршруты плагина `/api/continuous-agent.*`. Правка целей идёт маршрутом
  `tool` — **теми же инструментами, что у модели** (`continuous_add/edit/drop`): точечно
  и под запором реестра. Замену списка целиком (`goals`) снаружи не берут — она затирала
  бы правку из открытой формы.

Параллельность задают **моторы** доски: у модели N цепочек (по умолчанию четыре), и
заходы из разных рядов идут разом. Наши промпты — работа руками: они идут сразу, мимо
очереди доски.

Требования: хост `dsh --profile web` на этой машине и подключённый плагин
`dsh-continuous-agent` (claude-shell, `plugins/`) — без него работают только функции
`dsh_session`.

```python
from dsh_.dsh_board import dsh_board_session_new, dsh_board_ask, dsh_board_fanout

session = dsh_board_session_new('/home/me/proj', 'разбор логов')
answer = dsh_board_ask(session, 'найди ошибки в logs/', wait=True)
answers = dsh_board_fanout([{'cwd': '/p/a', 'title': 'a', 'text': '…'},
                            {'session': session, 'text': '…'}])
```
"""
import contextlib
import json
import os
import re
import threading
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT, DshApiError, \
    DshApiUnreachable, dsh_api_get, dsh_api_post
from dsh_.dsh_session import dsh_session_archive, dsh_session_cursor, dsh_session_list, \
    dsh_session_prompt, dsh_session_reply_wait, dsh_session_title

# Потолок ожидания ответа субагента по умолчанию, с.
DSH_BOARD_WAIT_TIMEOUT = 1800
# Шаг, с которым переспрашивать хост, не ответивший вовремя.
DSH_BOARD_RETRY_POLL = 2.0
# Кэш подписей карточек для `dsh_board_working` и сколько он живёт, с.
DSH_BOARD_TITLES = 'dsh-board.titles.json'
DSH_BOARD_TITLES_TTL = 600
# Потолок одного вызова плагина: заведение сессии собирает preset — это секунды.
DSH_BOARD_HTTP_TIMEOUT = 60

class DshBoardError(RuntimeError):
    """Отказ плагина доски или круг не кончился вовремя."""


def dsh_board_state(session: str = '', *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Снимок доски: цели, цепочки, занятые машины, настройки, проекты, архив.

    Args:
        session: окно, глазами которого смотреть; от него зависит только
            `sessionModel` — модель этого окна.

    Returns:
        `{goals, chain, machines, schedule, paused, executors, motors,
        models, catalog, workspaces, archived, sessionModel, now}`.
    """
    return dsh_api_get('continuous-agent.state', query=f'session={session or "-"}',
                       base=base, timeout=DSH_BOARD_HTTP_TIMEOUT)


def dsh_board_tool(session: str, name: str, args: dict, *,
                   base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Инструмент модели `name` от имени живой карточки `session`.

    Returns:
        Снимок доски после правки (как его видит модель).

    Raises:
        DshApiError: карточка не живая, инструмент неизвестен или отказал.
    """
    answer = dsh_api_post('continuous-agent.tool',
                          {'session': session, 'name': name, 'args': args or {}},
                          base=base, timeout=DSH_BOARD_HTTP_TIMEOUT) or {}
    return answer.get('value', {})


def dsh_board_settings(patch: dict, *, base: str = DSH_API_ADDR_DEFAULT) -> None:
    """Настройки доски: `{paused?, defaultQuota?, models?, motors?, executors?}`.

    ⚠ `models`, `motors`, `executors` — списки целиком, не добавка: что не назвал, то снял.
    """
    dsh_api_post('continuous-agent.settings', patch, base=base, timeout=DSH_BOARD_HTTP_TIMEOUT)


def dsh_board_model_of(session: str, *, base: str = DSH_API_ADDR_DEFAULT):
    """Модель, в бокс которой класть промпт карточки, или None — назвать нечем.

    Модель карточки (`sessionModel`) известна только после её первого круга: у свежей
    карточки выбора ещё нет. Тогда берётся включённая модель доски, если она одна, — ту
    же модель карточка и получит на первом круге. Промпт без модели лёг бы вне бокса
    («неположенным») и не пошёл бы нигде.
    """
    state = dsh_board_state(session, base=base)
    if state.get('sessionModel'):
        return state['sessionModel']
    models = state.get('models') or []
    return models[0] if len(models) == 1 else None


def dsh_board_working(*, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Кто из агентов сейчас работает и над чем — по занятым моторам доски.

    Источник истины — хост: маршрут плагина `continuous-agent.working` отдаёт занятые
    моторы с подписями одним лёгким запросом. Зовётся часто (статусная строка — раз в
    секунду). Плагин старше маршрута (404) — список собирается здесь: `state` доски и
    подписи из кэша на диске (`DSH_BOARD_TITLES`); тяжёлый `session/list` спрашивается,
    только когда подписи в кэше нет или он старше `DSH_BOARD_TITLES_TTL`.

    Returns:
        `[{session, title, kind, goal, motor, model, robot}]`: `kind` — `run` (заход промпта
        доски, `goal` — его название) или `hand` (работа руками: человек или
        оркестратор гонит сессию сам, `goal` пуст); `title` — подпись карточки.
    """
    try:
        answer = dsh_api_get('continuous-agent.working', base=base, timeout=DSH_BOARD_HTTP_TIMEOUT) or {}
        return [{key: row.get(key) for key in ('session', 'title', 'kind', 'goal', 'motor', 'model', 'robot')}
                for row in answer.get('working') or []]
    except DshApiError as error:
        if 'HTTP 404' not in str(error):
            raise
    # Плагин старше маршрута `working`: собираем сами — доска и подписи из кэша.
    state = dsh_board_state(base=base)
    working = [brick for brick in state.get('chain') or [] if brick.get('kind') in ('run', 'hand')]
    if not working:
        return []
    keys = [brick.get('session') or '' for brick in working]
    titles = _titles_cached(keys, base)
    return [{'session': key, 'title': titles.get(key, ''), 'kind': brick.get('kind'),
             'goal': brick.get('name') or '', 'motor': brick.get('motor'),
             'model': brick.get('model')} for key, brick in zip(keys, working)]


def dsh_board_motors_set(provider: str, model: str, count: int, *,
                         base: str = DSH_API_ADDR_DEFAULT) -> None:
    """Сколько моторов (параллельных цепочек) у модели: от 1 до 4, по умолчанию 4.

    Настройка `motors` правится списком целиком — что не назвал, то сбросил к умолчанию,
    — поэтому список берётся с доски и меняется в нём одна строка, как это делает форма.
    """
    current = [entry for entry in dsh_board_state(base=base).get('motors') or []
               if (entry.get('provider'), entry.get('model')) != (provider, model)]
    dsh_board_settings({'motors': current + [{'provider': provider, 'model': model,
                                              'motors': count}]}, base=base)


def dsh_board_sessions(include_idle: bool = False, *,
                       base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Карточки хоста для человека: без пустых и без архивных.

    Args:
        include_idle: и карточки без живого агента (одна история); по умолчанию только
            те, в которые можно слать промпт.

    Returns:
        `[{session, title, cwd, running, updatedAt}]`, свежие сверху.
    """
    archived = set(dsh_board_state(base=base).get('archived') or [])
    rows = []
    for item in dsh_session_list(base=base):
        if item.get('blank') or item.get('sessionId') in archived:
            continue
        if not include_idle and not item.get('agentAvailable'):
            continue
        rows.append({'session': item.get('sessionId'), 'title': dsh_session_title(item),
                     'cwd': item.get('cwd') or '', 'running': bool(item.get('running')),
                     'updatedAt': item.get('updatedAt')})
    return rows


def dsh_board_session_new(cwd: str, title: str, full_access: bool = False, robot: bool = True, *,
                          base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Завести карточку под каталог — как «＋ сессия» доски: preset и модель хоста.

    Args:
        robot: карточку заводит агент, а не человек — форма доски пометит её 🤖.
            Этот слой зовут оркестраторы, поэтому по умолчанию True.

    Returns:
        id новой сессии.

    Raises:
        DshBoardError: ручка ответила без id сессии.
    """
    answer = dsh_api_post('continuous-agent.session',
                          {'workspace': os.path.realpath(os.path.expanduser(cwd)),
                           'title': title, 'fullAccess': full_access, 'robot': robot},
                          base=base, timeout=DSH_BOARD_HTTP_TIMEOUT) or {}
    if not answer.get('session'):
        raise DshBoardError(f'карточка не заведена: ручка ответила {str(answer)[:200]}')
    return answer['session']


def dsh_board_ask(session: str, text: str, wait: bool = False,
                  timeout: float = DSH_BOARD_WAIT_TIMEOUT, images: list = None, *,
                  base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Задача субагенту: промпт в карточку; с `wait` — дождаться конца круга и ответа.

    Ответ — на **свой** промпт (`dsh_session_reply_wait`), а не первый конец круга после
    отправки: карточка могла быть занята чужим кругом.

    Args:
        wait: ждать конца круга и вернуть ответ; без него промпт встаёт в очередь
            карточки, и ответ потом — `dsh_session_answer_last`.
        timeout: потолок всего вызова, с — общий на засечку и ожидание ответа.
        images: пути к картинкам (скриншоты) — субагент видит их рядом с текстом.

    Returns:
        Текст ответа при `wait`, иначе пустая строка.

    Raises:
        DshBoardError: круг не кончился за `timeout`.
    """
    # Засечку можно спрашивать сколько угодно раз — хост, занятый первые секунды после
    # заведения карточки, не повод ронять задачу. Сам промпт вслепую не повторяем:
    # таймаут на отправке не говорит, дошёл ли он, а дважды он уйти не должен.
    deadline = time.time() + timeout
    mark = _patient(lambda: dsh_session_cursor(session, base=base), timeout)
    dsh_session_prompt(session, text, images=images, base=base)
    if not wait:
        return ''
    answer = dsh_session_reply_wait(session, mark, text, max(0.0, deadline - time.time()), base=base)
    if answer is None:
        raise DshBoardError(f'круг в {session} не кончился за {int(timeout)} с — ответ потом: '
                            f'dsh_session_answer_last')
    return answer


def dsh_board_fanout(tasks: list, timeout: float = DSH_BOARD_WAIT_TIMEOUT, *,
                     base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Раздать задачи субагентам разом и собрать ответы — в порядке задач.

    Задача — `{text, session}` (готовая карточка) или `{text, cwd, title}` (карточка
    заводится под каталог), с необязательным `images` — пути к картинкам; `archive: true` — убрать заведённую карточку в архив после
    ответа (после неудачи — не трогает: живой круг ещё можно дочитать). Задачи идут
    разом; несколько задач в одну карточку встают в её очередь, и у каждой — свой ответ.

    Returns:
        `[{session, answer, error}]` — у удавшейся `error` пустой; одна сорвавшаяся
        задача остальных не роняет.
    """
    results = [None] * len(tasks)

    def run(index: int, task: dict) -> None:
        session = task.get('session') or ''
        try:
            if not session:
                session = dsh_board_session_new(task['cwd'], task.get('title') or 'задача',
                                                bool(task.get('fullAccess')), base=base)
            answer = dsh_board_ask(session, task['text'], wait=True, timeout=timeout,
                                   images=task.get('images'), base=base)
            results[index] = {'session': session, 'answer': answer, 'error': ''}
        except Exception as error:   # noqa: BLE001 — контракт: у каждой задачи строка результата
            results[index] = {'session': session, 'answer': '', 'error': f'{type(error).__name__}: {error}'}
            return
        # Архив — только после ответа: сорвавшаяся задача могла оставить живой круг, и
        # архив оборвал бы его, а ответ из журнала ещё можно добрать.
        if task.get('archive') and session and not task.get('session'):
            with contextlib.suppress(DshApiError):
                dsh_session_archive(session, base=base)

    threads = [threading.Thread(target=run, args=(index, task), daemon=True)
               for index, task in enumerate(tasks)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def dsh_board_goal_session(goal_id: str, *, base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Карточка, у которой живёт цель доски: от её имени цель и правят."""
    for goal in dsh_board_state(base=base).get('goals', []):
        if goal.get('id') == goal_id:
            return goal.get('session') or ''
    raise DshBoardError(f'цели {goal_id} на доске нет')


# ── детали реализации ──

def _titles_cached(keys: list, base: str) -> dict:
    """Подписи карточек по ключам (без суффикса поколения) — из кэша, а нет — с хоста."""
    home = os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT)
    path = os.path.join(home, DSH_BOARD_TITLES)
    try:
        fresh = time.time() - os.stat(path).st_mtime < DSH_BOARD_TITLES_TTL
        with open(path, encoding='utf-8') as handle:
            titles = json.load(handle)
    except (OSError, ValueError):
        fresh, titles = False, {}
    if fresh and all(key in titles for key in keys):
        return titles
    titles = {}
    for item in dsh_session_list(base=base):
        key = re.sub(r'-g\d+$', '', item.get('sessionId') or '')
        titles.setdefault(key, dsh_session_title(item))
    with contextlib.suppress(OSError):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(titles, handle, ensure_ascii=False)
        os.replace(tmp, path)
    return titles


def _patient(ask, timeout: float):
    """Повторять безопасный вопрос хосту, пока он не ответит или не выйдет `timeout`.

    Повторяется только «хост не ответил» (`DshApiUnreachable`): отказ метода и 401
    приходят сразу, ждать их исправления нечего.
    """
    deadline = time.time() + timeout
    while True:
        try:
            return ask()
        except DshApiUnreachable:
            if time.time() > deadline:
                raise
            time.sleep(DSH_BOARD_RETRY_POLL)

if __name__ == '__main__':
    from dsh_.dsh_board_cli import dsh_board_cli_main
    raise SystemExit(dsh_board_cli_main())

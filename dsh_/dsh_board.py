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
import os
import threading
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DshApiError, DshApiUnreachable, \
    dsh_api_get, dsh_api_post
from dsh_.dsh_session import dsh_session_answer_last, dsh_session_archive, \
    dsh_session_cursor, dsh_session_list, dsh_session_prompt, dsh_session_title, \
    dsh_session_turn_wait

# Потолок ожидания ответа субагента по умолчанию, с.
DSH_BOARD_WAIT_TIMEOUT = 1800
# Шаг, с которым переспрашивать хост, не ответивший вовремя.
DSH_BOARD_RETRY_POLL = 2.0
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


def dsh_board_session_new(cwd: str, title: str, full_access: bool = False, *,
                          base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Завести карточку под каталог — как «＋ сессия» доски: preset и модель хоста.

    Returns:
        id новой сессии.
    """
    answer = dsh_api_post('continuous-agent.session',
                          {'workspace': os.path.realpath(os.path.expanduser(cwd)),
                           'title': title, 'fullAccess': full_access},
                          base=base, timeout=DSH_BOARD_HTTP_TIMEOUT) or {}
    return answer['session']


def dsh_board_ask(session: str, text: str, wait: bool = False,
                  timeout: float = DSH_BOARD_WAIT_TIMEOUT, *,
                  base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Задача субагенту: промпт в карточку; с `wait` — дождаться конца круга и ответа.

    Args:
        wait: ждать конца круга и вернуть ответ; без него промпт встаёт в очередь
            карточки, и ответ потом — `dsh_session_answer_last`.
        timeout: потолок ожидания конца круга, с.

    Returns:
        Текст ответа при `wait`, иначе пустая строка.

    Raises:
        DshBoardError: круг не кончился за `timeout`.
    """
    # Засечку можно спрашивать сколько угодно раз — хост, занятый первые секунды после
    # заведения карточки, не повод ронять задачу. Сам промпт вслепую не повторяем:
    # таймаут на отправке не говорит, дошёл ли он, а дважды он уйти не должен.
    mark = _patient(lambda: dsh_session_cursor(session, base=base), timeout)
    dsh_session_prompt(session, text, base=base)
    if not wait:
        return ''
    if not dsh_session_turn_wait(session, mark, 'turn/end', timeout, base=base):
        raise DshBoardError(f'круг в {session} не кончился за {int(timeout)} с — ответ потом: '
                            f'dsh_session_answer_last')
    return dsh_session_answer_last(session, mark, base=base)


def dsh_board_fanout(tasks: list, timeout: float = DSH_BOARD_WAIT_TIMEOUT, *,
                     base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Раздать задачи субагентам разом и собрать ответы — в порядке задач.

    Задача — `{text, session}` (готовая карточка) или `{text, cwd, title}` (карточка
    заводится под каталог); `archive: true` — убрать заведённую карточку в архив после
    ответа. Задачи идут разом — каждая в своей карточке.

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
            answer = dsh_board_ask(session, task['text'], wait=True, timeout=timeout, base=base)
            results[index] = {'session': session, 'answer': answer, 'error': ''}
        except (DshApiError, DshBoardError, KeyError) as error:
            results[index] = {'session': session, 'answer': '', 'error': str(error)}
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

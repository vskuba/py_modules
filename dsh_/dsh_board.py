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
import subprocess
import threading
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT, DshApiError, \
    DshApiUnavailable, DshApiUnreachable, dsh_api_call, dsh_api_get, dsh_api_post
from dsh_.dsh_saving import dsh_saving_owner_mark, dsh_saving_sent
from dsh_.dsh_worktree import DshWorktreeError, dsh_worktree_dirty, dsh_worktree_drop, dsh_worktree_merged, \
    dsh_worktree_new
from dsh_.dsh_session import dsh_session_archive, dsh_session_cursor, dsh_session_turn_start_wait, \
    dsh_session_dialog, dsh_session_find, dsh_session_history, dsh_session_list, \
    dsh_session_prompt, dsh_session_title, dsh_session_turn_reply

# Потолок ожидания ответа субагента по умолчанию, с.
DSH_BOARD_WAIT_TIMEOUT = 1800
# Шаг, с которым переспрашивать хост, не ответивший вовремя.
DSH_BOARD_RETRY_POLL = 2.0
# Сколько раз продолжать круг, оборванный перезапуском хоста, и чем.
DSH_BOARD_RESUME_TRIES = 2
DSH_BOARD_RESUME_TEXT = ('Хост перезапустился посреди твоего круга, и он оборвался. Продолжай '
                         'задачу с того места, где остановился, и дай полный ответ.')
# Сколько держать замок fanout после отправки: пока круг задачи не начнётся, с.
DSH_BOARD_START_HOLD = 60
# Сколько ждать, пока у хоста после перезапуска поднимутся службы, с.
DSH_BOARD_STARTUP_WAIT = 120
# Выжимка ответа: просьба к субагенту и пределы, которыми её режем, если он не послушал.
DSH_BOARD_BRIEF_ASK = ('\n\nФОРМАТ ОТВЕТА ОБЯЗАТЕЛЕН: первая строка ответа — «ИТОГ:», за ней 3–5 '
                       'коротких строк сути (не длиннее 600 символов вместе), затем пустая строка и '
                       'подробности. Никаких вступлений до «ИТОГ:». Оркестратор читает только итог.')
DSH_BOARD_BRIEF_LINES = 5
DSH_BOARD_BRIEF_CHARS = 700
# Уборка: сколько дней тишины у агентской карточки, чтобы её убрать, и примета копии репо.
DSH_BOARD_GC_DAYS = 1
# Как часто уборка зовётся сама (статусная строка) и где метка последнего раза.
DSH_BOARD_GC_EVERY = 3600
DSH_BOARD_GC_STAMP = 'dsh-board.gc-stamp.json'
DSH_BOARD_WORKTREE_MARK = '/.claude/worktrees/dsh-'
# Задание проверяющему субагенту.
DSH_BOARD_VERIFY_ASK = (
    'Ты — проверяющий. Другой агент получил задачу и дал ответ. Сверь КАЖДОЕ утверждение '
    'ответа с кодом в этой папке или с первоисточником (для фактов из интернета — открой '
    'источник). Ничего не меняй в файлах. Разложи утверждения на три группы: ✅ подтверждено, '
    '❌ неверно (что на самом деле и где это видно), ❓ не удалось проверить (почему). '
    'Не пересказывай ответ — только вердикты.\n\n'
    '=== ЗАДАЧА ===\n{task}\n\n=== ОТВЕТ ===\n{answer}'
)
# Моторов у модели, которой их не назначали (как `MOTOR_COUNT_DEFAULT` плагина).
DSH_BOARD_MOTORS_DEFAULT = 4
# Сколько самых затратных карточек показывать в статистике.
DSH_BOARD_STATS_TOP = 5
# Отметки «ответ прочитан» по агентским карточкам: ключ карточки → число кругов.
DSH_BOARD_INBOX = 'dsh-board.inbox-turns.json'
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

    Returns:
        `[{session, title, kind, goal, motor, model, robot}]` — см. `dsh_board_motors_busy`.
    """
    return dsh_board_motors_busy(base=base)['working']


def dsh_board_motors_busy(*, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Занятые моторы и сколько их всего: `{capacity, working}` — для «DSH 1/4».

    Источник истины — хост: маршрут плагина `continuous-agent.working` отдаёт занятые
    моторы с подписями и ёмкость (моторы включённых моделей) одним лёгким запросом.
    Зовётся часто (статусная строка — раз в секунду). Плагин старше маршрута (404) или
    ёмкости — список и ёмкость собираются здесь: `state` доски и подписи из кэша на диске
    (`DSH_BOARD_TITLES`); тяжёлый `session/list` — только для незнакомых карточек.

    Returns:
        `{capacity, working: [{session, title, kind, goal, motor, model, robot}]}`:
        `kind` — `run` (заход промпта доски) или `hand` (работа руками), `title` — подпись.
    """
    keys = ('session', 'title', 'kind', 'goal', 'motor', 'model', 'robot')
    try:
        answer = dsh_api_get('continuous-agent.working', base=base, timeout=DSH_BOARD_HTTP_TIMEOUT) or {}
        working = [{key: row.get(key) for key in keys} for row in answer.get('working') or []]
        if 'capacity' in answer:
            return {'capacity': answer['capacity'], 'working': working}
        return {'capacity': _capacity(dsh_board_state(base=base)), 'working': working}
    except DshApiError as error:
        if 'HTTP 404' not in str(error):
            raise
    # Плагин старше маршрута `working`: собираем сами — доска и подписи из кэша.
    state = dsh_board_state(base=base)
    bricks = [brick for brick in state.get('chain') or [] if brick.get('kind') in ('run', 'hand')]
    session_keys = [brick.get('session') or '' for brick in bricks]
    titles = _titles_cached(session_keys, base) if bricks else {}
    working = [{'session': key, 'title': titles.get(key, ''), 'kind': brick.get('kind'),
                'goal': brick.get('name') or '', 'motor': brick.get('motor'), 'model': brick.get('model'),
                'robot': bool(brick.get('robot'))} for key, brick in zip(session_keys, bricks)]
    return {'capacity': _capacity(state), 'working': working}


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


def dsh_board_session_new(cwd: str, title: str, full_access: bool = True, robot: bool = True, *,
                          base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Завести карточку под каталог — как «＋ сессия» доски: preset и модель хоста.

    Args:
        full_access: «Full Access Permission» — карточка работает без подтверждения прав.
            По умолчанию True, как галочка окна доски: без неё субагент встаёт на первом
            же `git commit` в копии репо (каталог .git вне песочницы) и ждёт человека.
        robot: карточку заводит агент, а не человек — форма доски пометит её 🤖.
            Этот слой зовут оркестраторы, поэтому по умолчанию True.

    Returns:
        id новой сессии.

    Raises:
        DshBoardError: ручка ответила без id сессии.
    """
    body = {'workspace': os.path.realpath(os.path.expanduser(cwd)),
            'title': title, 'fullAccess': full_access, 'robot': robot}
    answer = _patient(lambda: dsh_api_post('continuous-agent.session', body, base=base,
                                           timeout=DSH_BOARD_HTTP_TIMEOUT),
                      DSH_BOARD_STARTUP_WAIT, retry=(DshApiUnavailable,)) or {}
    if not answer.get('session'):
        raise DshBoardError(f'карточка не заведена: ручка ответила {str(answer)[:200]}')
    # Чья карточка — для счёта экономии по сессиям Claude Code (dsh_saving).
    dsh_saving_owner_mark(answer['session'])
    return answer['session']


def dsh_board_ask(session: str, text: str, wait: bool = False,
                  timeout: float = DSH_BOARD_WAIT_TIMEOUT, images: list = None, gate=None, *,
                  base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Задача субагенту: промпт в карточку; с `wait` — дождаться конца круга и ответа.

    Ответ — на **свой** промпт (`dsh_session_turn_reply`), а не первый конец круга после
    отправки: карточка могла быть занята чужим кругом. Круг, оборванный перезапуском хоста
    (`turn/end` с причиной `interrupted`), продолжается сам — до `DSH_BOARD_RESUME_TRIES` раз.

    Args:
        wait: ждать конца круга и вернуть ответ; без него промпт встаёт в очередь
            карточки, и ответ потом — `dsh_session_answer_last`.
        timeout: потолок всего вызова, с — общий на засечку и ожидание ответа.
        images: пути к картинкам (скриншоты) — субагент видит их рядом с текстом.
        gate: общий замок для нескольких задач разом (`fanout`): задача ждёт свободный
            мотор и держит замок, пока её круг не начнётся, — две задачи не займут один
            и тот же свободный мотор. Без него промпт уходит сразу.

    Returns:
        Текст ответа при `wait`, иначе пустая строка.

    Raises:
        DshBoardError: круг не кончился за `timeout`.
    """
    # Засечку можно спрашивать сколько угодно раз — хост, занятый первые секунды после
    # заведения карточки, не повод ронять задачу. Сам промпт вслепую не повторяем:
    # таймаут на отправке не говорит, дошёл ли он, а дважды он уйти не должен.
    deadline = time.time() + timeout
    dsh_saving_sent(session, text)
    mark = _patient(lambda: dsh_session_cursor(session, base=base), timeout)
    # Промпт повторяем только на «служба ещё не поднялась» — тогда он точно не ушёл.
    def send():
        _patient(lambda: dsh_session_prompt(session, text, images=images, base=base), timeout,
                 retry=(DshApiUnavailable,))

    if gate is None:
        send()
    else:
        with gate:
            dsh_board_motor_wait(max(0.0, deadline - time.time()), base=base)
            send()
            dsh_session_turn_start_wait(session, mark, DSH_BOARD_START_HOLD, base=base)
    if not wait:
        return ''
    asked = text
    for resumed in range(DSH_BOARD_RESUME_TRIES + 1):
        reply = dsh_session_turn_reply(session, mark, asked, max(0.0, deadline - time.time()), base=base)
        if reply is None:
            raise DshBoardError(f'круг в {session} не кончился за {int(timeout)} с — ответ потом: '
                                f'dsh-board inbox')
        if reply['reason'] != 'interrupted' or resumed == DSH_BOARD_RESUME_TRIES:
            break
        # Хост перезапустился посреди круга и закрыл его: промежуточная реплика — не
        # ответ. Просим продолжить с того же места, и ждём уже конца нового круга.
        mark, asked = reply['seq'], DSH_BOARD_RESUME_TEXT
        _patient(lambda: dsh_session_prompt(session, asked, base=base), DSH_BOARD_STARTUP_WAIT,
                 retry=(DshApiUnavailable,))
    answer = reply['text']
    if reply['reason'] == 'interrupted':
        raise DshBoardError(f'круг в {session} обрывался {DSH_BOARD_RESUME_TRIES + 1} раза — '
                            f'последняя реплика: {dsh_board_brief(answer)}')
    # Ответ получен — во «входящих» его больше нет.
    with contextlib.suppress(DshApiError, OSError):
        dsh_board_inbox_take(session, dsh_board_turns(dsh_session_find(session, base=base)))
    return answer


def dsh_board_inbox(*, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Агентские карточки с ответами, которых оркестратор ещё не забрал.

    Ответ субагента остаётся в журнале его карточки, даже если сессия оркестратора,
    отдавшая задачу, закрылась. Здесь — такие: агентская карточка (`robots` доски), не в
    архиве, круг не идёт, а кругов (`sessionStats.turns`) больше, чем отмечено
    прочитанным (`dsh_board_inbox_take`). Счёт по кругам, а не по журналу: служебные
    записи после ответа (заголовок, метки) журнал сдвигают, а круг — нет.

    Returns:
        `[{session, title, turns, brief}]` — `turns` отдавать в `dsh_board_inbox_take`;
        `brief` — начало последнего ответа из `turnOutline` (без загрузки журнала).
    """
    return [row for row in _inbox_scan(base) if row is not None]


def dsh_board_inbox_count(*, base: str = DSH_API_ADDR_DEFAULT) -> int:
    """Сколько агентских карточек с непрочитанным — для статусной строки."""
    return len(_inbox_scan(base))


def dsh_board_resume(dry: bool = False, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Продолжить агентские круги, оборванные перезапуском хоста, — у карточек, которых никто не ждёт.

    Перезапуск хоста закрывает идущий круг записью `turn/end` с причиной `interrupted`.
    Карточку, которую ждёт `dsh_board_ask`, он продолжит сам; здесь — остальные: агентская
    карточка, не в архиве, круг не идёт, и последний конец круга в журнале — оборванный.

    Args:
        dry: только показать, ничего не отправлять.

    Returns:
        `[{session, title}]` — продолженные (или найденные, при `dry`).
    """
    state = dsh_board_state(base=base)
    robots = set(state.get('robots') or [])
    archived = set(state.get('archived') or [])
    rows = []
    for item in dsh_session_list(base=base):
        session = item.get('sessionId') or ''
        key = re.sub(r'-g\d+$', '', session)
        if key not in robots or session in archived or item.get('running'):
            continue
        ends = [event for event in dsh_session_history(session, 2, base=base)
                if event.get('type') == 'turn/end']
        reason = (((ends[-1].get('data') or {}).get('reason')) or {}).get('kind') if ends else ''
        if reason != 'interrupted':
            continue
        if not dry:
            dsh_session_prompt(session, DSH_BOARD_RESUME_TEXT, base=base)
        rows.append({'session': key, 'title': dsh_session_title(item)})
    return rows


def dsh_board_verify(session: str, timeout: float = DSH_BOARD_WAIT_TIMEOUT, *,
                     base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Перекрёстная проверка: второй субагент сверяет ответ первого с кодом и источниками.

    Проверка — самая дорогая часть работы оркестратора с субагентами: каждое утверждение
    надо сверить самому. Здесь её делает другой агент в той же папке, без контекста
    первого (свежим взглядом): получает исходную задачу и ответ, сверяет каждое
    утверждение и раскладывает на «подтверждено / неверно / не проверить». Оркестратору —
    итог; смотреть самому остаётся только «неверно» и «не проверить».

    Returns:
        `{session, verifier, answer}` — проверяемая карточка, карточка проверяющего и его
        полный ответ (итог — `dsh_board_brief`).

    Raises:
        DshBoardError: в проверяемой карточке нет пары «задача — ответ».
    """
    item = dsh_session_find(session, base=base)
    rows = dsh_session_dialog(session, 10, base=base)
    task = next((row['text'] for row in reversed(rows) if row['role'] == 'user'), '')
    answer = next((row['text'] for row in reversed(rows) if row['role'] == 'assistant'), '')
    if not task or not answer:
        raise DshBoardError(f'в карточке {session} нет пары «задача — ответ» для проверки')
    title = f"проверка: {dsh_session_title(item) or session}"[:60]
    verifier = dsh_board_session_new(item.get('cwd') or '.', title, base=base)
    prompt = DSH_BOARD_VERIFY_ASK.format(task=task, answer=answer) + DSH_BOARD_BRIEF_ASK
    return {'session': session, 'verifier': verifier,
            'answer': dsh_board_ask(verifier, prompt, wait=True, timeout=timeout, base=base)}


def dsh_board_gc(days: float = DSH_BOARD_GC_DAYS, dry: bool = False, *,
                 base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Убрать отработавшие агентские карточки: архив, их копии репозитория, проекты копий.

    Каждое «d …» и каждая задача оркестратора заводит карточку; без уборки их через неделю
    десятки. Убираются только агентские (`robots`) и не идущие:
    - **сразу** — карточка копии, чей каталог уже удалён или чья ветка уже слита в основную
      (`dsh_worktree_merged`): работа принята, ждать нечего;
    - остальные — после `days` тишины.
    Копия снимается, только если в ней нет незакоммиченных правок — ветка остаётся.
    Проекты копий, чей каталог удалён мимо слоя (`git worktree remove`), снимаются с панели
    тоже — и без карточек: иначе их список на панели только растёт.
    Зовётся сама — `dsh_board_gc_auto` из статусной строки, не чаще раза в час.

    Args:
        days: сколько дней тишины, чтобы карточка считалась отработавшей.
        dry: только показать.

    Returns:
        `[{session, title, worktree, kept}]` — `kept` непусто, если копию оставили (почему).
    """
    state = dsh_board_state(base=base)
    robots = set(state.get('robots') or [])
    archived = set(state.get('archived') or [])
    edge = (time.time() - days * 86400) * 1000
    rows = []
    for item in dsh_session_list(base=base):
        session = item.get('sessionId') or ''
        key = re.sub(r'-g\d+$', '', session)
        if key not in robots or session in archived or item.get('running'):
            continue
        cwd = item.get('cwd') or ''
        worktree = cwd if DSH_BOARD_WORKTREE_MARK in cwd else ''
        gone = bool(worktree) and not os.path.isdir(worktree)
        accepted = bool(worktree) and not gone and _merged(worktree)
        if not (gone or accepted) and (item.get('updatedAt') or 0) > edge:
            continue
        kept = ''
        if worktree and not gone and dsh_worktree_dirty(worktree):
            kept = 'в копии незакоммиченные правки'
        worktree_drop = bool(worktree) and not gone and not kept
        if not dry:
            dsh_session_archive(session, base=base)
            if worktree_drop:
                with contextlib.suppress(DshWorktreeError):
                    dsh_worktree_drop(worktree)
            if worktree and not kept:
                with contextlib.suppress(DshApiError):
                    dsh_board_workspace_drop(worktree, base=base)
        rows.append({'session': key, 'title': dsh_session_title(item), 'worktree': worktree, 'kept': kept})
    # Проекты копий без каталога — и без карточек тоже: удалённая мимо слоя копия.
    for workspace in state.get('workspaces') or []:
        path = workspace.get('path') or ''
        if DSH_BOARD_WORKTREE_MARK in path and not os.path.isdir(path):
            if not dry:
                with contextlib.suppress(DshApiError):
                    dsh_api_call('workspace/delete', {'request': {'workspaceId': workspace['id']}}, base=base)
            rows.append({'session': '', 'title': os.path.basename(path), 'worktree': path, 'kept': ''})
    return rows


def dsh_board_gc_auto(*, base: str = DSH_API_ADDR_DEFAULT) -> bool:
    """Уборка сама, не чаще `DSH_BOARD_GC_EVERY` секунд на машину — для статусной строки.

    Метка времени — файл в каталоге хоста; кто первым застолбил срок, тот и убирает, прочие
    выходят сразу. Сбой уборки не роняет зовущего.

    Returns:
        True — уборка прошла в этом вызове.
    """
    stamp = _home_path(DSH_BOARD_GC_STAMP)
    try:
        if time.time() - os.path.getmtime(stamp) < DSH_BOARD_GC_EVERY:
            return False
    except OSError:
        pass
    _json_write(stamp, {'at': time.time()})
    try:
        dsh_board_gc(base=base)
    except (DshApiError, DshBoardError, OSError):
        return False
    return True


def dsh_board_workspace_drop(path: str, *, base: str = DSH_API_ADDR_DEFAULT) -> bool:
    """Снять проект с панели хоста по его каталогу (файлы и сессии остаются).

    Returns:
        True — проект был и снят; False — такого проекта на панели нет.
    """
    real = os.path.realpath(path)
    for workspace in dsh_board_state(base=base).get('workspaces') or []:
        if os.path.realpath(workspace.get('path') or '') == real:
            dsh_api_call('workspace/delete', {'request': {'workspaceId': workspace['id']}}, base=base)
            return True
    return False


def dsh_board_stats(all_sessions: bool = False, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Сколько токенов отработали субагенты — работа, которую иначе сделал бы оркестратор.

    Считает проекцию `tokenUsage`, которую хост держит у каждой карточки: входные без
    кэша, выходные, чтение и запись кэша. По умолчанию — только агентские карточки
    (`robots` доски, архивные тоже: работа уже сделана).

    Args:
        all_sessions: считать все карточки хоста, а не только агентские.

    Returns:
        `{sessions, uncachedInputTokens, outputTokens, cacheReadTokens, cacheWriteTokens,
        top: [{session, title, tokens}]}` — `top` по убыванию входных+выходных.
    """
    robots = set(dsh_board_state(base=base).get('robots') or [])
    keys = ('uncachedInputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens')
    total = dict.fromkeys(keys, 0)
    rows = []
    for item in dsh_session_list(base=base):
        key = re.sub(r'-g\d+$', '', item.get('sessionId') or '')
        if not all_sessions and key not in robots:
            continue
        usage = ((item.get('projections') or {}).get('values') or {}).get('tokenUsage') or {}
        if not usage:
            continue
        for name in keys:
            total[name] += int(usage.get(name) or 0)
        rows.append({'session': key, 'title': dsh_session_title(item),
                     'tokens': int(usage.get('uncachedInputTokens') or 0) + int(usage.get('outputTokens') or 0)})
    rows.sort(key=lambda row: row['tokens'], reverse=True)
    return {'sessions': len(rows), **total, 'top': rows[:DSH_BOARD_STATS_TOP]}


def dsh_board_inbox_take(session: str, turns: int) -> None:
    """Отметить ответы карточки до круга номер `turns` прочитанными."""
    key = re.sub(r'-g\d+$', '', session)
    taken = _inbox_read()
    if turns > taken.get(key, 0):
        taken[key] = turns
        _json_write(_home_path(DSH_BOARD_INBOX), taken)


def dsh_board_turns(item: dict) -> int:
    """Сколько кругов у карточки (`sessionStats.turns` из `session/list`)."""
    return int((((item.get('projections') or {}).get('values') or {}).get('sessionStats') or {}).get('turns') or 0)


def dsh_board_brief(answer: str, limit: int = DSH_BOARD_BRIEF_CHARS) -> str:
    """Выжимка ответа субагента для оркестратора — чтобы полный ответ не ел его контекст.

    Если ответ начинается блоком «ИТОГ:» (так просит `DSH_BOARD_BRIEF_ASK`), берётся этот
    блок до первой пустой строки; иначе — первые строки ответа. В обоих случаях не
    длиннее `limit` символов.
    """
    text = (answer or '').strip()
    # После «ИТОГ» — только разметка (`**`, `:`), не первый знак самого итога (`код`).
    match = re.search(r'(?im)^\W*итог[*_]*\s*:?[*_]*[ \t]*(.+?)(?:\n\s*\n|\Z)', text, re.DOTALL)
    brief = match.group(1).strip() if match else '\n'.join(text.splitlines()[:DSH_BOARD_BRIEF_LINES])
    if len(brief) <= limit:
        return brief
    # Режем по концу последнего предложения до предела, а не посреди слова: обрубок
    # «вердикт…» оркестратор не прочтёт, а неполный список предложений — прочтёт.
    cut = brief[:limit - 1]
    end = max(cut.rfind(mark) for mark in ('. ', '! ', '? ', '\n', '.\n'))
    return (cut[:end + 1] if end > limit // 3 else cut).rstrip() + ' …'


def dsh_board_motor_wait(timeout: float = DSH_BOARD_WAIT_TIMEOUT, *,
                         base: str = DSH_API_ADDR_DEFAULT) -> None:
    """Дождаться свободного мотора: занятых меньше, чем всего (`dsh_board_motors_busy`).

    Работа руками (наши промпты) моторами доски не гейтится — без этой проверки десять
    задач `fanout` разом ушли бы в модель, у которой слотов меньше, и встали бы в её
    очередь, деля скорость. Хост, не ответивший вовремя, считается занятым.

    Raises:
        DshBoardError: мотор не освободился за `timeout`.
    """
    started = time.time()
    while True:
        try:
            status = dsh_board_motors_busy(base=base)
            if len(status['working']) < max(1, int(status['capacity'] or 0)):
                return
        except DshApiError:
            pass
        if time.time() - started > timeout:
            raise DshBoardError(f'свободный мотор не появился за {int(timeout)} с')
        time.sleep(DSH_BOARD_RETRY_POLL)


def dsh_board_fanout(tasks: list, timeout: float = DSH_BOARD_WAIT_TIMEOUT, on_start=None, on_done=None, *,
                     base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Раздать задачи субагентам разом и собрать ответы — в порядке задач.

    Задача — `{text, session}` (готовая карточка) или `{text, cwd, title}` (карточка
    заводится под каталог), с необязательными `images` — пути к картинкам — и `worktree: true`
    — задача правит код в своей копии репозитория (`dsh_worktree`), в результате путь и ветка; `archive: true` — убрать заведённую карточку в архив после
    ответа (после неудачи — не трогает: живой круг ещё можно дочитать). Задачи идут
    разом, но не больше, чем свободных моторов: лишние ждут (`dsh_board_motor_wait`).
    Несколько задач в одну карточку встают в её очередь, и у каждой — свой ответ.

    Args:
        on_start: `(index, row)` — задача получила карточку (`row`: session, worktree,
            branch); зовётся до промпта, чтобы оборванный `fanout` можно было дособрать.
        on_done: `(index, row)` — задача кончилась; зовётся сразу, а не после всех.
            Сбой обоих колбэков задачу не роняет.

    Returns:
        `[{session, answer, error}]` — у удавшейся `error` пустой; одна сорвавшаяся
        задача остальных не роняет.
    """
    results = [None] * len(tasks)

    def tell(callback, index, row):
        if callback:
            with contextlib.suppress(Exception):
                callback(index, row)
    # Общий замок задач: каждая ждёт свободный мотор — задач больше, чем моторов, не
    # уходит в модель разом; лишние ждут, пока освободится мотор.
    gate = threading.Lock()

    def run(index: int, task: dict) -> None:
        session = task.get('session') or ''
        tree = {}
        try:
            if not session:
                cwd = task['cwd']
                if task.get('worktree'):
                    # Пишущей задаче — своя копия репо: соседи по fanout не затрут друг друга.
                    tree = dsh_worktree_new(cwd, task.get('title') or 'задача')
                    cwd = tree['path']
                session = dsh_board_session_new(cwd, task.get('title') or 'задача',
                                                bool(task.get('fullAccess', True)), base=base)
            tell(on_start, index, {'session': session, **({'worktree': tree['path'], 'branch': tree['branch']}
                                                         if tree else {})})
            answer = dsh_board_ask(session, task['text'], wait=True, timeout=timeout,
                                   images=task.get('images'), gate=gate, base=base)
            results[index] = {'session': session, 'answer': answer, 'error': '',
                              **({'worktree': tree['path'], 'branch': tree['branch']} if tree else {})}
        except Exception as error:   # noqa: BLE001 — контракт: у каждой задачи строка результата
            results[index] = {'session': session, 'answer': '', 'error': f'{type(error).__name__}: {error}'}
            tell(on_done, index, results[index])
            return
        tell(on_done, index, results[index])
        # Архив — только после ответа: сорвавшаяся задача могла оставить живой круг, и
        # архив оборвал бы его, а ответ из журнала ещё можно добрать.
        if task.get('archive') and session and not task.get('session') and not tree:
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

def _merged(path: str) -> bool:
    try:
        return dsh_worktree_merged(path)
    except (DshWorktreeError, OSError, subprocess.SubprocessError):
        return False


def _capacity(state: dict) -> int:
    """Моторы включённых моделей по настройкам доски (запасной путь для старого плагина)."""
    motors = {(row.get('provider'), row.get('model')): row.get('motors') for row in state.get('motors') or []}
    models = state.get('models') or []
    if not models:
        return DSH_BOARD_MOTORS_DEFAULT
    return sum(int(motors.get((pair.get('provider'), pair.get('model'))) or DSH_BOARD_MOTORS_DEFAULT)
               for pair in models)


def _home_path(name: str) -> str:
    """Путь к файлу слоя в каталоге хоста (`DSH_HOME`, по умолчанию `~/.dsh`)."""
    return os.path.join(os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT), name)


def _json_write(path: str, value) -> None:
    """Записать JSON целиком через временный файл: читающий не увидит полфайла."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _inbox_scan(base: str) -> list:
    """Непрочитанные агентские карточки: `{session, title, turns, brief}` на каждую."""
    state = dsh_board_state(base=base)
    robots = set(state.get('robots') or [])
    archived = set(state.get('archived') or [])
    taken = _inbox_read()
    rows = []
    for item in dsh_session_list(base=base):
        key = re.sub(r'-g\d+$', '', item.get('sessionId') or '')
        if key not in robots or item.get('sessionId') in archived or item.get('running'):
            continue
        turns = dsh_board_turns(item)
        if turns <= taken.get(key, 0):
            continue
        outline = ((item.get('projections') or {}).get('values') or {}).get('turnOutline') or []
        last = (outline[-1].get('response') if outline else '') or ''
        rows.append({'session': key, 'title': dsh_session_title(item), 'turns': turns,
                     'brief': dsh_board_brief(last)})
    return rows


def _inbox_read() -> dict:
    try:
        with open(_home_path(DSH_BOARD_INBOX), encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _titles_cached(keys: list, base: str) -> dict:
    """Подписи карточек по ключам (без суффикса поколения) — из кэша, а нет — с хоста."""
    path = _home_path(DSH_BOARD_TITLES)
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
        _json_write(path, titles)
    return titles


def _patient(ask, timeout: float, retry: tuple = (DshApiUnreachable, DshApiUnavailable)):
    """Повторять вопрос хосту, пока он не ответит или не выйдет `timeout`.

    По умолчанию повторяются «хост не ответил» и «служба ещё не поднялась» — так зовут
    безопасные вопросы (засечку). Действия (промпт, заведение карточки) зовут с
    `retry=(DshApiUnavailable,)`: после таймаута неизвестно, дошло ли действие, и повтор
    задвоил бы его. Отказ метода и 401 приходят сразу, ждать их исправления нечего.
    """
    deadline = time.time() + timeout
    while True:
        try:
            return ask()
        except retry:
            if time.time() > deadline:
                raise
            time.sleep(DSH_BOARD_RETRY_POLL)

if __name__ == '__main__':
    from dsh_.dsh_board_cli import dsh_board_cli_main
    raise SystemExit(dsh_board_cli_main())

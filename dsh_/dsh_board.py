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

Параллельность задаёт **бокс доски**: задача, поставленная с ожиданием (`dsh_board_ask`
с `wait`, `dsh_board_fanout`), — одноразовая цель на доске, видимая и переставляемая;
освободившийся мотор модели берёт раннюю по порядку бокса. Потому одна очередь у всех:
и у человека из формы, и у оркестратора.

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
import fcntl
import json
import os
import re
import subprocess
import threading
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT, DshApiError, \
    DshApiUnavailable, DshApiUnreachable, dsh_api_call, dsh_api_get, dsh_api_post
from dsh_.dsh_saving import dsh_saving_owner_mark, dsh_saving_sent
from dsh_.dsh_wait import dsh_wait_done, dsh_wait_expect
from dsh_.dsh_worktree import DshWorktreeError, dsh_worktree_dirty, dsh_worktree_drop, dsh_worktree_merged, \
    dsh_worktree_new
from dsh_.dsh_session import dsh_session_archive, dsh_session_cursor, dsh_session_dialog, \
    dsh_session_find, dsh_session_history, dsh_session_image_part, dsh_session_list, \
    dsh_session_prompt, dsh_session_title, dsh_session_turn_reply

# Потолок ожидания ответа субагента по умолчанию, с.
DSH_BOARD_WAIT_TIMEOUT = 1800
# Шаг, с которым переспрашивать хост, не ответивший вовремя.
DSH_BOARD_RETRY_POLL = 2.0
# Сколько раз продолжать круг, оборванный перезапуском хоста, и чем.
DSH_BOARD_RESUME_TRIES = 2
DSH_BOARD_RESUME_TEXT = ('The host restarted in the middle of your turn and cut it off. Continue the '
                         'task from where you stopped and give the full answer.')
# Сколько ждать, пока у хоста после перезапуска поднимутся службы, с.
DSH_BOARD_STARTUP_WAIT = 120
# Выжимка ответа: просьба к субагенту и пределы, которыми её режем, если он не послушал.
# ⚠ Тексты, уходящие субагенту, — по-английски: дешевле в токенах (~17% на Qwen3.8, замер
# 24.11) и привычнее модели. Оркестратор пересказывает человеку по-русски сам.
DSH_BOARD_BRIEF_ASK = ('\n\nANSWER FORMAT IS MANDATORY: write the answer in English. The first line is '
                       '"SUMMARY:", followed by 3-5 short lines of substance (600 characters max in '
                       'total), then an empty line and the details. Nothing before "SUMMARY:". The '
                       'orchestrator reads only the summary.')
DSH_BOARD_BRIEF_LINES = 5
# Пишущей задаче в своей копии — как прогнать тесты именно на её коде: свой одноразовый
# сервер над копией (`project_worktree_test`), не общий стенд с кодом основной выкладки.
DSH_BOARD_WORKTREE_TEST_ASK = (
    '\n\nTESTS ON YOUR CODE. Your repository copy is {path}. The shared stand (:8011) runs the '
    'main checkout, not your code: checking your change there is pointless. Your own server over '
    'the copy and the tests against it are one command (the server starts and stops by itself, '
    'running in parallel with others is fine):\n'
    '  PYTHONPATH={layer} {python} -m project_.project_worktree test '
    'tests/test_<what_you_changed>.py --worktree {path}\n'
    'Run the test files related to your change, plus new tests if you wrote any. Exit code 0 '
    'means green. Do NOT touch the shared task server (:8013, task_server.sh, project_worktree '
    'up/down): the pipeline QA works on it. In the answer: what you ran and how many passed; if '
    'you did not run tests, say why.')
DSH_BOARD_BRIEF_CHARS = 700
# Уборка: сколько дней тишины у агентской карточки, чтобы её убрать, и примета копии репо.
DSH_BOARD_GC_DAYS = 1
# Как часто уборка зовётся сама (статусная строка) и где метка последнего раза.
DSH_BOARD_GC_EVERY = 3600
DSH_BOARD_GC_STAMP = 'dsh-board.gc-stamp.json'
# Автопродолжение оборванных кругов: как часто, какие свежие, что уже продолжено, кого ждут.
DSH_BOARD_REVIVE_EVERY = 60
DSH_BOARD_REVIVE_RECENT = 1800
DSH_BOARD_REVIVE_STAMP = 'dsh-board.revive-stamp.json'
DSH_BOARD_REVIVED = 'dsh-board.revived.json'
DSH_BOARD_WAITING = 'dsh-board.waiting.json'
DSH_BOARD_WORKTREE_MARK = '/.claude/worktrees/dsh-'
# Технический workspace карточек-копий: одна папка каталога хоста (с ней же —
# `dsh-worktrees` в панели), под которой живут ВСЕ worktree-карточки. Иначе каждая
# `--worktree`-задача плодит на панели новый «проект»: харнесс не умеет задать сессии
# cwd отдельно от пути workspace — `session/create` принимает workspaceId или cwd
# (не оба), а `WorkspaceEntity.attachSession` отвергает сессию, чей cwd не равен пути
# workspace. Путь копии агенту даёт шапка промпта (`dsh_board_worktree_head`).
DSH_BOARD_WORKTREE_WORKSPACE = 'dsh-worktrees'
DSH_BOARD_WORKTREES = 'dsh-board.worktrees.json'   # карточка → {path, branch, root} копии
DSH_BOARD_WORKTREE_HEAD = ('Your repository copy: {path}. Start every command in it with '
                           '`cd {path}`; open files only by absolute paths inside it{tail}.\n\n')
DSH_BOARD_WORKTREE_README = (
    '# dsh-worktrees\n\n'
    'Технический workspace доски `dsh-board`: здесь живут карточки всех пишущих задач '
    '`new --worktree` и `fanout worktree` — одной папкой, а не отдельным проектом на '
    'панели на каждую. Самих копий репозитория здесь нет: у каждой задачи своя копия '
    'внутри её репозитория (`<repo>/.claude/worktrees/`), путь копии и основной репозиторий '
    '— в шапке промпта карточки. Здесь не правят файлы и не запускают команды; папку не '
    'удаляют — она общий дом этих карточек.\n')
# Задание проверяющему субагенту.
DSH_BOARD_VERIFY_ASK = (
    'You are a verifier. Another agent got a task and gave an answer. Check EVERY claim of the '
    'answer against the code in this folder or against the primary source (for facts from the '
    'internet, open the source). Do not change any files. Sort the claims into three groups: '
    '✅ confirmed, ❌ wrong (what is actually true and where it is visible), ❓ could not verify '
    '(why). Do not retell the answer, give only verdicts. Write in English.\n\n'
    '=== TASK ===\n{task}\n\n=== ANSWER ===\n{answer}'
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
                          worktree: dict = None, base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Завести карточку под каталог — как «＋ сессия» доски: preset и модель хоста.

    Args:
        full_access: «Full Access Permission» — карточка работает без подтверждения прав.
            По умолчанию True, как галочка окна доски: без неё субагент встаёт на первом
            же `git commit` в копии репо (каталог .git вне песочницы) и ждёт человека.
        robot: карточку заводит агент, а не человек — форма доски пометит её 🤖.
            Этот слой зовут оркестраторы, поэтому по умолчанию True.
        worktree: `{path, branch, root}` копии из `dsh_worktree_new`: карточка встаёт не в
            копию, а в общий технический workspace (`DSH_BOARD_WORKTREE_WORKSPACE`) — одна
            задача, один проект на панели; путь копии ложится в метаданные
            (`dsh_board_worktree_row`), а агенту его даёт шапка промпта
            (`dsh_board_worktree_head`). Пусто — карточка под `cwd`, как раньше.

    Returns:
        id новой сессии.

    Raises:
        DshBoardError: ручка ответила без id сессии.
    """
    # Хост не принимает cwd отдельно от workspace (see DSH_BOARD_WORKTREE_WORKSPACE),
    # поэтому копией карточки рулит только промпт, а workspace у всех них — общий.
    place = os.path.realpath(_worktree_workspace(make=True)) if worktree and worktree.get('path') \
        else os.path.realpath(os.path.expanduser(cwd))
    body = {'workspace': place, 'title': title, 'fullAccess': full_access, 'robot': robot}
    answer = _patient(lambda: dsh_api_post('continuous-agent.session', body, base=base,
                                           timeout=DSH_BOARD_HTTP_TIMEOUT),
                      DSH_BOARD_STARTUP_WAIT, retry=(DshApiUnavailable,)) or {}
    if not answer.get('session'):
        raise DshBoardError(f'карточка не заведена: ручка ответила {str(answer)[:200]}')
    if worktree and worktree.get('path'):
        _worktrees_mark(answer['session'], worktree)
    # Чья карточка — для счёта экономии по сессиям Claude Code (dsh_saving).
    dsh_saving_owner_mark(answer['session'])
    return answer['session']


def dsh_board_worktree_row(session: str) -> dict:
    """Метаданные копии карточки (`{path, branch, root, at}`), заведённой с `--worktree`.

    Ключ — карточка без суффикса поколения; у старой карточки (есть до метаданных) их нет,
    и её копия узнаётся по её же cwd — см. `DSH_BOARD_WORKTREE_MARK`.
    """
    return (_json_read(_home_path(DSH_BOARD_WORKTREES)) or {}).get(_key(session)) or {}


def dsh_board_worktree_forget(session: str) -> None:
    """Забыть метаданные копии: карточка отработала (копию приняли или сняли)."""
    path = _home_path(DSH_BOARD_WORKTREES)
    rows = _json_read(path) or {}
    if rows.pop(_key(session), None) is not None:
        _json_write(path, rows)


def dsh_board_ask(session: str, text: str, wait: bool = False,
                  timeout: float = DSH_BOARD_WAIT_TIMEOUT, images: list = None, *,
                  base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Задача субагенту — всегда через бокс доски; `wait` решает только, ждать ли ответа.

    Задача кладётся в бокс доски тем же `continuous_add`, каким человек кладёт
    промпты из формы, — от имени самой карточки (`quotaRounds: 1`, без периода). Пока
    она в боксе — она видна на доске целью: её можно переставить, снять, поставить на
    её место другую; первый свободный мотор цепочки достаётся ранней по порядку бокса.
    Ответ ждётся на **свой** промпт (`dsh_session_turn_reply`): карточка могла быть
    занята чужим кругом. Полученный ответ — цель снимается с доски, бокс не должен
    копить закрытое; при ошибке и таймауте цель не трогается: живой круг и его ответ
    разберёт человек (`dsh-board answer`, «входящие»). Круг, оборванный перезапуском
    хоста (`interrupted`), продолжается по-прежнему прямым промптом — до
    `DSH_BOARD_RESUME_TRIES` раз.

    Без `wait` — тот же клад, ответ забирает `dsh-board wait` (метка `dsh_wait_expect`),
    он же снимает отработавшую цель (`dsh_board_box_clear`). ⚠ Прямым промптом мимо
    бокса без `wait` было нельзя: такой круг занимает мотор «работой руками» сверх
    числа моторов — 24.11 так встал пятый мотор при четырёх.

    Args:
        wait: ждать конца круга и вернуть ответ; без него промпт встаёт в очередь
            карточки, и ответ потом — `dsh_session_answer_last`.
        timeout: потолок всего вызова, с — общий на засечку, клад и ожидание ответа.
        images: пути к картинкам (скриншоты) — едут вместе с задачей и в круге
            лягут частями промпта рядом с текстом.

    Returns:
        Текст ответа при `wait`, иначе пустая строка.

    Raises:
        DshBoardError: круг не кончился за `timeout`.

    ⚠ Два одинаковых текста в одну карточку неразличимы: ни боксом, ни журналом.
    """
    deadline = time.time() + timeout
    dsh_saving_sent(session, text)
    goal_id, mark = _box_place(session, text, images=images, deadline=deadline, base=base)
    if not wait:
        dsh_wait_expect(session)
        return ''
    # Ответ этой карточки ждёт сессия Claude, отдавшая задачу: без метки ответ, не
    # дождавшийся здесь, ушёл бы в никуда (dsh_wait).
    dsh_wait_expect(session)
    return _box_answer(session, goal_id, mark, text, deadline, base=base)


def dsh_board_box_clear(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Снять с доски отработавшие (`done`) цели карточки — ответ на них уже забран.

    Задачу без ожидания кладут в бокс, а ответ забирает другой процесс (`dsh-board
    wait`); он и зовёт уборку. Закрытая одноразовая цель в очередь не встаёт, но
    копится списком — потому снимаем. Сбой молчит: ответ уже в руках.

    Returns:
        id снятых целей.
    """
    dropped = []
    with contextlib.suppress(DshApiError, OSError):
        for goal in dsh_board_state(session, base=base).get('goals') or []:
            if _key(goal.get('session') or '') == _key(session) and goal.get('status') == 'done':
                dsh_board_tool(session, 'continuous_drop', {'goal': goal['id']}, base=base)
                dropped.append(goal['id'])
    return dropped


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


def dsh_board_resume(dry: bool = False, recent: float = None, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Продолжить агентские круги, оборванные перезапуском хоста, — у карточек, которых никто не ждёт.

    Перезапуск хоста закрывает идущий круг записью `turn/end` с причиной `interrupted`.
    Карточку, которую ждёт `dsh_board_ask`, он продолжит сам; здесь — остальные: агентская
    карточка, не в архиве, круг не идёт, и последний конец круга в журнале — оборванный.

    Каждый обрыв продолжается **один раз**: номер его `turn/end` запоминается
    (`DSH_BOARD_REVIVED`), второй вызов его не тронет. Карточку, которую сейчас ждёт
    `dsh_board_ask` (`_waiting`), пропускаем — он продолжит её сам, иначе ушло бы два промпта.

    Args:
        dry: только показать, ничего не отправлять.
        recent: только карточки, обновлявшиеся за столько секунд: давний обрыв могли
            бросить сознательно. None — все.

    Returns:
        `[{session, title}]` — продолженные (или найденные, при `dry`).
    """
    state = dsh_board_state(base=base)
    robots = set(state.get('robots') or [])
    archived = set(state.get('archived') or [])
    edge = (time.time() - recent) * 1000 if recent else 0
    waited = dsh_board_waited()
    revived = _json_read(_home_path(DSH_BOARD_REVIVED)) or {}
    rows = []
    for item in dsh_session_list(base=base):
        session = item.get('sessionId') or ''
        key = re.sub(r'-g\d+$', '', session)
        if key not in robots or session in archived or item.get('running') or key in waited:
            continue
        if (item.get('updatedAt') or 0) < edge:
            continue
        ends = [event for event in dsh_session_history(session, 2, base=base)
                if event.get('type') == 'turn/end']
        reason = (((ends[-1].get('data') or {}).get('reason')) or {}).get('kind') if ends else ''
        if reason != 'interrupted' or revived.get(key) == ends[-1].get('seq'):
            continue
        if not dry:
            dsh_session_prompt(session, DSH_BOARD_RESUME_TEXT, base=base)
            revived[key] = ends[-1].get('seq')
            _json_write(_home_path(DSH_BOARD_REVIVED), revived)
        rows.append({'session': key, 'title': dsh_session_title(item)})
    return rows


def dsh_board_revive_auto(*, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Продолжить свежие обрывы сами — раз в `DSH_BOARD_REVIVE_EVERY` секунд на машину.

    Зовёт статусная строка: после перезапуска хоста агентские круги поднимаются без
    человека. Смотрятся только карточки, обновлявшиеся за `DSH_BOARD_REVIVE_RECENT`, —
    обычно их ноль, и вызов почти ничего не стоит. Сбой не роняет зовущего.
    """
    stamp = _home_path(DSH_BOARD_REVIVE_STAMP)
    try:
        if time.time() - os.path.getmtime(stamp) < DSH_BOARD_REVIVE_EVERY:
            return []
    except OSError:
        pass
    _json_write(stamp, {'at': time.time()})
    try:
        return dsh_board_resume(recent=DSH_BOARD_REVIVE_RECENT, base=base)
    except (DshApiError, DshBoardError, OSError):
        return []


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
    row = dsh_board_worktree_row(session)
    if row.get('path'):
        # Копия: проверяющий — в тот же технический workspace, но глазами в копию (шапка).
        verifier = dsh_board_session_new(row['path'], title, worktree=row, base=base)
        prompt = dsh_board_worktree_head(row) + DSH_BOARD_VERIFY_ASK.format(task=task, answer=answer)
    else:
        verifier = dsh_board_session_new(item.get('cwd') or '.', title, base=base)
        prompt = DSH_BOARD_VERIFY_ASK.format(task=task, answer=answer)
    prompt += DSH_BOARD_BRIEF_ASK
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
        # Копия новой карточки — в метаданных (её cwd теперь технический workspace),
        # старой — по cwd с приметой копии.
        worktree = dsh_board_worktree_row(key).get('path') or \
            (cwd if DSH_BOARD_WORKTREE_MARK in cwd else '')
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
                dsh_board_worktree_forget(key)   # копии больше нет — метаданные не в счёт
        rows.append({'session': key, 'title': dsh_session_title(item), 'worktree': worktree, 'kept': kept})
    # Проекты копий без каталога — и без карточек тоже: удалённая мимо слоя копия.
    tech = os.path.realpath(_worktree_workspace())   # технический workspace не снимаем
    for workspace in state.get('workspaces') or []:
        path = workspace.get('path') or ''
        if os.path.realpath(path) == tech:
            continue
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

    Raises:
        DshApiError: хост отказал в удалении.
    """
    real = os.path.realpath(path)
    if real == os.path.realpath(_worktree_workspace()):
        return False   # технический workspace — общий дом карточек-копий, его не снимаем
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

    Если ответ начинается блоком «SUMMARY:» (так просит `DSH_BOARD_BRIEF_ASK`; старые
    ответы — «ИТОГ:»), берётся этот
    блок до первой пустой строки; иначе — первые строки ответа. В обоих случаях не
    длиннее `limit` символов.
    """
    text = (answer or '').strip()
    # После «ИТОГ» — только разметка (`**`, `:`), не первый знак самого итога (`код`).
    match = re.search(r'(?im)^\W*(?:итог|summary)[*_]*\s*:?[*_]*[ \t]*(.+?)(?:\n\s*\n|\Z)', text, re.DOTALL)
    brief = match.group(1).strip() if match else '\n'.join(text.splitlines()[:DSH_BOARD_BRIEF_LINES])
    if len(brief) <= limit:
        return brief
    # Режем по концу последнего предложения до предела, а не посреди слова: обрубок
    # «вердикт…» оркестратор не прочтёт, а неполный список предложений — прочтёт.
    cut = brief[:limit - 1]
    end = max(cut.rfind(mark) for mark in ('. ', '! ', '? ', '\n', '.\n'))
    return (cut[:end + 1] if end > limit // 3 else cut).rstrip() + ' …'


def dsh_board_fanout(tasks: list, timeout: float = DSH_BOARD_WAIT_TIMEOUT, on_start=None, on_done=None, *,
                     base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Раздать задачи субагентам разом и собрать ответы — в порядке задач.

    Каждая задача кладётся в бокс доски — `continuous_add` от имени своей карточки,
    `quotaRounds: 1`, без периода, — и по порядку списка: что раньше в задачах, то
    раньше в боксе, и первый свободный мотор достаётся ранней. Задачи видны на доске
    карточками: их можно переставить, снять, поставить на их место другие. Нити только
    ждут ответы, задачу не заводит и не гонит ни одна нить — заход делает доска, и
    доходит он, даже если процесс, раздавший задачи, умер (ответ — `dsh-board answer`,
    `collect`).

    Задача — `{text, session}` (готовая карточка) или `{text, cwd, title}` (карточка
    заводится под каталог), с необязательными `images` — пути к картинкам — и `worktree: true`
    — задача правит код в своей копии репозитория (`dsh_worktree`), в результате путь и ветка; `archive: true` — убрать заведённую карточку в архив после
    ответа (после неудачи — не трогает: живой круг ещё можно дочитать). Задач больше,
    чем моторов, — лишние ждут своей очереди в боксе, а не в нити. Несколько задач в
    одну карточку лежат в её боксе по порядку и у каждой — свой ответ.

    Args:
        on_start: `(index, row)` — задача получила карточку (`row`: session, worktree,
            branch); зовётся до клада в бокс, чтобы оборванный `fanout` можно было
            дособрать.
        on_done: `(index, row)` — задача кончилась; зовётся сразу, а не после всех.
            Сбой обоих колбэков задачу не роняет.

    Returns:
        `[{session, answer, error}]` — у удавшейся `error` пустой; одна сорвавшаяся
        задача остальных не роняет.
    """
    results = [None] * len(tasks)
    plans = []

    def tell(callback, index, row):
        if callback:
            with contextlib.suppress(Exception):
                callback(index, row)

    def wait(plan: dict) -> None:
        """Дождаться ответа одной задачи — в нити, больше в нити делать нечего."""
        index, task, session, tree = plan['index'], plan['task'], plan['session'], plan['tree']
        try:
            answer = _box_answer(session, plan['goal'], plan['mark'], plan['text'],
                                 time.time() + timeout, base=base)
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

    # Клады — последовательно, по порядку задач: порядок в боксе — порядок раздачи,
    # и гонка нитей не должна его путать. Ждать — параллельно, это только чтение.
    for index, task in enumerate(tasks):
        session, tree, text = task.get('session') or '', {}, task['text']
        try:
            if not session:
                cwd = task['cwd']
                if task.get('worktree'):
                    # Пишущей задаче — своя копия репо: соседи по fanout не затрут друг друга.
                    tree = dsh_worktree_new(cwd, task.get('title') or 'задача')
                    cwd = tree['path']
                    text = dsh_board_worktree_head(tree) + text + dsh_board_worktree_test_ask(tree)
                session = dsh_board_session_new(cwd, task.get('title') or 'задача',
                                                bool(task.get('fullAccess', True)), base=base,
                                                worktree=tree or None)
            tell(on_start, index, {'session': session, **({'worktree': tree['path'], 'branch': tree['branch']}
                                                         if tree else {})})
            dsh_saving_sent(session, text)
            goal_id, mark = _box_place(session, text, images=task.get('images'),
                                       deadline=time.time() + timeout, base=base)
            dsh_wait_expect(session)
            plans.append({'index': index, 'task': task, 'session': session, 'tree': tree,
                          'goal': goal_id, 'mark': mark, 'text': text})
        except Exception as error:   # noqa: BLE001 — контракт: у каждой задачи строка результата
            results[index] = {'session': session, 'answer': '', 'error': f'{type(error).__name__}: {error}'}
            tell(on_done, index, results[index])

    threads = [threading.Thread(target=wait, args=(plan,), daemon=True) for plan in plans]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def dsh_board_worktree_head(tree: dict) -> str:
    """Шапка в начало промпта задачи о копии: где копия и куда не соваться.

    Карточка копии живёт в техническом workspace, её cwd про копию не знает — путь
    доносит только промпт. Пустая строка, если пути копии нет (не worktree-задача).
    """
    path = tree.get('path') or ''
    if not path:
        return ''
    root = tree.get('root') or ''
    tail = f'; do not touch the main repository {root}' if root else ''
    return DSH_BOARD_WORKTREE_HEAD.format(path=path, tail=tail)


def dsh_board_worktree_test_ask(tree: dict) -> str:
    """Строка к промпту пишущей задачи: как прогнать тесты на коде своей копии.

    Пусто, если в проекте нет общего слоя с `project_worktree` — подсказывать нечего.
    """
    # ⚠ Слой — тот, откуда загружен сам dsh_board, а не `py_modules` проекта: чекаут проекта
    # бывает старше, и команды `test` в нём нет — субагент тогда собирал круг руками на
    # общем сервере задачи (:8013), который делит с QA конвейера.
    layer = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isfile(os.path.join(layer, 'project_', 'project_worktree.py')):
        return ''
    root = tree.get('root') or ''
    venv = os.path.join(root, '.venv', 'bin', 'python3')
    return DSH_BOARD_WORKTREE_TEST_ASK.format(path=tree['path'], layer=layer,
                                              python=venv if os.path.exists(venv) else 'python3')


def dsh_board_waited() -> set:
    """Карточки, ответ которых прямо сейчас ждёт живой процесс (`ask --wait`, `fanout`)."""
    rows = _json_read(_home_path(DSH_BOARD_WAITING)) or {}
    return {key for key, pid in rows.items() if _pid_alive(pid)}


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


def _box_place(session: str, text: str, images: list = None, deadline: float = None, *,
               base: str = DSH_API_ADDR_DEFAULT) -> tuple:
    """Задачу — в бокс доски от имени карточки: модель, засечка журнала, `continuous_add`.

    Клад — тем же инструментом `continuous_add`, каким человек кладёт промпты из
    формы, и строго в собственном имени карточки: цель живёт на её сессии, в её
    боксе, подписана её моделью (`dsh_board_model_of`), заход одноразовый
    (`quotaRounds: 1`, без периода). Засечку журнала берём ДО клада: круг может
    начаться через секунду после добавления, и ответ ищется от засечки.

    Прямым промптом вместо клада — два случая, где бокс задачу не донёс бы: карточка
    спит (инструмент хост даёт только живому агенту) и модель карточки нечем назвать
    при нескольких моделях на доске (цель не лежала бы ни на какой цепочке).

    Returns:
        `(id цели, засечка журнала)`; id пустой — задача ушла прямым промптом.

    Raises:
        DshBoardError: клад прошёл, а цели в снимке доски не видно.
    """
    def left() -> float:
        return max(0.0, deadline - time.time())

    model = _patient(lambda: dsh_board_model_of(session, base=base), left())
    mark = _patient(lambda: dsh_session_cursor(session, base=base), left())
    if model is None:
        _patient(lambda: dsh_session_prompt(session, text, images=images, base=base), left(),
                 retry=(DshApiUnavailable,))
        return '', mark
    args = {'text': text, 'model': model, 'everyMinutes': 0, 'quotaRounds': 1}
    if images:
        args['images'] = [_box_image(path) for path in images]
    try:
        value = _patient(lambda: dsh_board_tool(session, 'continuous_add', args, base=base), left(),
                         retry=(DshApiUnavailable,))
    except DshApiError as error:
        if 'not live' not in str(error):   # карточка спит: инструмент живёт только у живого агента
            raise
        _patient(lambda: dsh_session_prompt(session, text, images=images, base=base), left(),
                 retry=(DshApiUnavailable,))
        return '', mark
    goal_id = _box_goal_id(value, session, text)
    if goal_id == '':
        raise DshBoardError(f'клад в {session} прошёл, а цели в снимке доски нет — '
                            f'ответ потом: dsh-board answer')
    return goal_id, mark


def _box_goal_id(value: dict, session: str, text: str) -> str:
    """Id цели клада из снимка доски: последняя цель этой карточки с этим текстом.

    Плагин нормализует текст цели (срезает пробелы по краям), потому сверка —
    сжатием пробелов, а не посимвольно; карточка в снимке подписана без поколения,
    потому и здесь ключ, а не строка сессии.
    """
    rows = [goal for goal in value.get('goals') or []
            if _key(goal.get('session') or '') == _key(session)
            and ' '.join(str(goal.get('text') or '').split()) == ' '.join(text.split())]
    return (rows[-1].get('id') or '') if rows else ''


def _box_image(path: str) -> dict:
    """Картинку — формой цели: `{mediaType, data, name}`; частью промпта круг её соберёт сам."""
    part = dsh_session_image_part(path)
    return {name: part[name] for name in ('mediaType', 'data', 'name')}


def _box_answer(session: str, goal_id: str, mark: int, asked: str, deadline: float, *,
                base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Дождаться ответа на задачу; полученный — убрать цель с доски.

    Ждёт свой круг (`dsh_session_turn_reply`); круг, оборванный перезапуском хоста,
    продолжает по-прежнему прямым промптом — цель тут ни при чём, очередь она не
    ждёт, обрыв — дело живого круга. Таймаут и обрывы подряд — отказ, цель не
    тронута: круг её живой, его ответ разберёт человек (`dsh-board answer`).

    ⚠ `mark` — засечка журнала ДО клада; `goal_id` пустой — задача шла прямым
    промптом, и снимать на доске нечего.

    Raises:
        DshBoardError: круг не кончился в срок или обрывался слишком много раз.
    """
    started = time.time()
    # Пока ждём — карточка помечена: обрыв продолжаем здесь, `revive --auto` её не трогает.
    with _waiting(session):
        for resumed in range(DSH_BOARD_RESUME_TRIES + 1):
            reply = dsh_session_turn_reply(session, mark, asked, max(0.0, deadline - time.time()), base=base)
            if reply is None:
                raise DshBoardError(f'круг в {session} не кончился за {int(time.time() - started)} с — '
                                    f'ответ потом: dsh-board inbox')
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
    # Ответ получен — цель отработала, закрытой в боксе не место. Ошибку снимаем молча:
    # ответ уже в руках, а одноразовая цель закрывается и сама.
    if goal_id:
        with contextlib.suppress(DshApiError, OSError):
            dsh_board_tool(session, 'continuous_drop', {'goal': goal_id}, base=base)
    # Во «входящих» ответа больше нет, и ждать его больше некому.
    with contextlib.suppress(DshApiError, OSError):
        dsh_board_inbox_take(session, dsh_board_turns(dsh_session_find(session, base=base)))
    with contextlib.suppress(OSError):
        dsh_wait_done(session)
    return answer


@contextlib.contextmanager
def _waiting(session: str):
    """Пометка «эту карточку ждёт процесс N» на время ожидания ответа."""
    key = re.sub(r'-g\d+$', '', session)
    with contextlib.suppress(OSError):
        _waiting_edit(lambda rows: rows.__setitem__(key, os.getpid()))
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            _waiting_edit(lambda rows: rows.pop(key) if rows.get(key) == os.getpid() else None)


def _waiting_edit(change) -> None:
    """Правка списка ждущих под файловым замком: его пишут потоки fanout и чужие процессы."""
    path = _home_path(DSH_BOARD_WAITING)
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = _json_read(path) or {}
        change(rows)
        _json_write(path, rows)


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _json_read(path: str):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _key(session: str) -> str:
    """Ключ карточки без суффикса поколения (`-gN`) — им карточку ищут в файлах слоя."""
    return re.sub(r'-g\d+$', '', session or '')


def _worktree_workspace(make: bool = False) -> str:
    """Каталог технического workspace карточек-копий — в каталоге хоста (`DSH_HOME`).

    С `make` — создать каталог и README, если их нет: хост заводит workspace только
    по существующему каталогу, а пустой каталог вне репозитория — и есть задумка
    (промах по относительному пути падает здесь, а не пишет в чужое дерево).
    """
    path = _home_path(DSH_BOARD_WORKTREE_WORKSPACE)
    if make:
        os.makedirs(path, exist_ok=True)
        readme = os.path.join(path, 'README.md')
        if not os.path.isfile(readme):
            with open(readme, 'w', encoding='utf-8') as handle:
                handle.write(DSH_BOARD_WORKTREE_README)
    return path


def _worktrees_mark(session: str, tree: dict) -> None:
    """Записать метаданные копии под карточку: land/diff/gc читают их, а не cwd."""
    path = _home_path(DSH_BOARD_WORKTREES)
    rows = _json_read(path) or {}
    rows[_key(session)] = {'path': tree.get('path') or '', 'branch': tree.get('branch') or '',
                           'root': tree.get('root') or '', 'at': time.time()}
    _json_write(path, rows)


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
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'   # писателей бывает несколько разом
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

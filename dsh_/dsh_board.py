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

**Потолок хоста** (`maxParallel` доски) считает все занятые сессии: заходы доски, работу
руками и наши промпты. Бегунок доски соблюдает его сам; `dsh_board_ask` — здесь: место
проверяется и промпт отправляется под файловой блокировкой, которая держится, пока круг
не начнётся. Иначе два вызова, пущенные в одну секунду, оба увидели бы «занято 0».

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
import os
import threading
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT, DshApiError, \
    dsh_api_get, dsh_api_post
from dsh_.dsh_session import dsh_session_answer_last, dsh_session_archive, \
    dsh_session_cursor, dsh_session_list, dsh_session_prompt, dsh_session_title, \
    dsh_session_turn_wait

# Потолок ожидания ответа субагента по умолчанию, с.
DSH_BOARD_WAIT_TIMEOUT = 1800
# Сколько держать блокировку места после отправки: пока в журнале не ляжет начало круга.
# Дольше — значит промпт встал в очередь карточки за чужим, и места он не занял.
DSH_BOARD_SLOT_HOLD = 30
# Шаг опроса места под потолком.
DSH_BOARD_SLOT_POLL = 2.0
# Потолок одного вызова плагина: заведение сессии собирает preset — это секунды.
DSH_BOARD_HTTP_TIMEOUT = 60
# Файл блокировки места — один на машину, как и хост.
DSH_BOARD_LOCK_NAME = 'dsh-board.lock'


class DshBoardError(RuntimeError):
    """Отказ плагина доски, нет места под потолком или круг не кончился вовремя."""


def dsh_board_state(session: str = '', *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Снимок доски: цели, цепочки, занятые машины, настройки, проекты, архив.

    Args:
        session: окно, глазами которого смотреть; от него зависит только
            `sessionModel` — модель этого окна.

    Returns:
        `{goals, chain, machines, schedule, maxParallel, paused, executors, motors,
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
    """Настройки доски: `{maxParallel?, paused?, defaultQuota?, models?, motors?, executors?}`.

    ⚠ `models`, `motors`, `executors` — списки целиком, не добавка: что не назвал, то снял.
    """
    dsh_api_post('continuous-agent.settings', patch, base=base, timeout=DSH_BOARD_HTTP_TIMEOUT)


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


def dsh_board_slot_wait(session: str, timeout: float = DSH_BOARD_WAIT_TIMEOUT, *,
                        base: str = DSH_API_ADDR_DEFAULT) -> None:
    """Дождаться места под потолком хоста, прежде чем занять карточку `session`.

    Карточка, уже занятая работой, места не требует: промпт встанет в её очередь и новой
    параллельности не добавит. Хост, не ответивший вовремя, считается занятым.

    Raises:
        DshBoardError: место не освободилось за `timeout`.
    """
    started = time.time()
    while True:
        try:
            state = dsh_board_state(session, base=base)
        except DshApiError:
            state = None
        if state is not None:
            limit = state.get('maxParallel') or 0
            busy = [machine.get('busySession') for machine in state.get('machines', [])]
            if limit == 0 or session in busy or len(busy) < limit:
                return
        if time.time() - started > timeout:
            raise DshBoardError(f'место под потолком хоста не освободилось за {int(timeout)} с')
        time.sleep(DSH_BOARD_SLOT_POLL)


def dsh_board_ask(session: str, text: str, wait: bool = False,
                  timeout: float = DSH_BOARD_WAIT_TIMEOUT, limit: bool = True, *,
                  base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Задача субагенту: промпт в карточку; с `wait` — дождаться конца круга и ответа.

    Args:
        wait: ждать конца круга и вернуть ответ; без него промпт встаёт в очередь
            карточки, и ответ потом — `dsh_session_answer_last`.
        timeout: потолок ожидания места и конца круга, с.
        limit: соблюдать потолок хоста (`maxParallel`); False — отправить сразу.

    Returns:
        Текст ответа при `wait`, иначе пустая строка.

    Raises:
        DshBoardError: нет места под потолком или круг не кончился за `timeout`.
    """
    mark = dsh_session_cursor(session, base=base)
    if limit:
        with _slot_lock():
            dsh_board_slot_wait(session, timeout, base=base)
            dsh_session_prompt(session, text, base=base)
            dsh_session_turn_wait(session, mark, 'turn/start', DSH_BOARD_SLOT_HOLD, base=base)
    else:
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
    ответа. Параллельность ограничивает потолок хоста: задачи сверх него ждут места.

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

@contextlib.contextmanager
def _slot_lock():
    """Один процесс за раз проверяет место и занимает его — на всю машину."""
    home = os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT)
    with open(os.path.join(home, DSH_BOARD_LOCK_NAME), 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


if __name__ == '__main__':
    from dsh_.dsh_board_cli import dsh_board_cli_main
    raise SystemExit(dsh_board_cli_main())

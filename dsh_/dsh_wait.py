"""Ответ субагента не уходит в никуда: оркестратор ждёт его фоном и просыпается по нему.

Оркестратор (Claude Code) будят только **его** фоновые процессы: кончился процесс — пришло
уведомление. Задача, отданная субагенту без ожидания (`new --ask` без `--wait`, оборванный
`fanout`, круг, продолжённый `revive`), такого процесса не имеет — субагент заканчивает,
а оркестратор, закрывший ход, об этом не узнаёт.

Поэтому каждый промпт субагенту из сессии Claude Code ставит метку «ответ ждём»
(`dsh_wait_expect`, владелец — `CLAUDE_CODE_SESSION_ID`). Снимает её любой забор ответа из
«входящих» (`dsh_board_inbox_take`: `ask --wait`, `answer`, `inbox --take`, сам ждун).
`dsh-board wait` — фоновый ждун: дожидается ответов по всем меткам своей сессии,
кладёт их в файлы, печатает итоги и выходит — выход и есть будильник. Застава на `Stop`
(`claude_hook_dsh_wait`) не отпускает ход, пока метки есть, а живого ждуна нет.

⚠ Раньше метку снимали только `ask --wait` и ждун: ответ, забранный `dsh-board answer`
(хоть субагентом, разбирающим чужие карточки), оставлял метку, и ждун висел до
`DSH_WAIT_MAX_AGE`, не будя оркестратора и по уже собранным ответам (2026-11-26).
⚠ Новый ждун сессии гасит прежнего (`_waiter_replace`): ждун, переживший перезапуск
харнеса, будить уже некого, а копились они по три-пять на сессию.

```bash
dsh-board wait --out-dir <каталог>      # фоном (run_in_background): разбудит по ответам
```

⚠ Обрыв (`turn/end interrupted`) — не ответ: его продолжит `revive --auto`, ждун ждёт дальше.
⚠ Ждёт по «входящим» на кэше списка карточек (60 с) — задержка до минуты, зато без тяжёлых
вызовов в цикле: они однажды уже уложили хост.
"""
import fcntl
import json
import os
import re
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT, DshApiError

DSH_WAIT_FILE = 'dsh-board.expected.json'
DSH_WAIT_OWNER_ENV = 'CLAUDE_CODE_SESSION_ID'
DSH_WAIT_POLL = 20.0
# Метка старше — субагент или хост пропали насовсем; ждать её вечно нельзя.
DSH_WAIT_MAX_AGE = 12 * 3600


def dsh_wait_expect(session: str, owner: str = None) -> None:
    """Пометить: из сессии `owner` (по умолчанию — из окружения) ждут ответа этой карточки."""
    owner = owner or os.environ.get(DSH_WAIT_OWNER_ENV) or ''
    if owner and session:
        _edit(lambda rows: rows.__setitem__(_key(session), {'owner': owner, 'at': time.time()}))


def dsh_wait_done(session: str) -> None:
    """Ответ карточки забран — метку снять."""
    _edit(lambda rows: rows.pop(_key(session), None))


def dsh_wait_pending(owner: str) -> list:
    """Карточки, ответа которых ждёт сессия `owner`; старые метки (`DSH_WAIT_MAX_AGE`) — мимо."""
    return sorted(dsh_wait_marks(owner))


def dsh_wait_marks(owner: str) -> dict:
    """Метки «ждём ответ», выставленные сессией: {карточка: {'owner', 'at'}}; старые — мимо.

    `at` (минута отдачи) нужен заставе «жди живого субагента» (`claude_gate_dsh_alive`),
    чтобы сказать человеку, сколько ждёт каждая карточка.
    """
    edge = time.time() - DSH_WAIT_MAX_AGE
    return {key: row for key, row in _read().items()
            if isinstance(row, dict) and row.get('owner') == owner and row.get('at', 0) > edge}


def dsh_wait_owner_of(session: str) -> str:
    """Сессия, которая сейчас ждёт ответа карточки (последней дала ей задание `ask`); нет
    свежей метки — пусто.

    ⚠ Не то же, что `dsh_saving_owner_of` («кто завёл»): другая сессия может отправить
    карточке новое задание — тогда ответ её, а заводила прежняя.
    """
    row = _read().get(_key(session))
    edge = time.time() - DSH_WAIT_MAX_AGE
    return str(row.get('owner') or '') if isinstance(row, dict) and row.get('at', 0) > edge else ''


def dsh_wait_key(session: str) -> str:
    """Ключ карточки в метках и во «входящих»: id без суффикса поколения (`-gN`).

    Им сверяют id карточки из `dsh_board_sessions` или с занятых моторов со списком меток;
    та же нормализация инлайном живёт в `dsh_board.py`.
    """
    return _key(session)


def dsh_wait_waiter_alive(owner: str) -> bool:
    """Ждёт ли сейчас живой процесс `dsh-board wait` этой сессии."""
    try:
        with open(_waiter_path(owner), encoding='utf-8') as handle:
            os.kill(int(handle.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def dsh_wait_answered(sessions: list, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Из карточек `sessions` — те, чей ответ уже лежит непрочитанным; обрывы — мимо.

    Признак «ответ есть» один у ждуна и у заставы «жди живого субагента»
    (`claude_gate_dsh_alive`): карточка во «входящих» `dsh_board_inbox` (круг кончился,
    ответ не забран) и последний её `turn/end` — не `interrupted`: обрыв продолжит
    `revive --auto`, ответа нет.

    ⚠ «Входящие» недоступны — пустой список: пока хост молчит, про ответ не известно.
    ⚠ Сбой `dsh_session_history` не глотается — как в цикле ждуна до вынесения сюда.
    """
    from dsh_.dsh_board import dsh_board_inbox  # ленивый: цикл импорта
    from dsh_.dsh_session import dsh_session_history

    want = {_key(key) for key in sessions or []}

    try:
        rows = [row for row in dsh_board_inbox(base=base) if _key(row['session']) in want]
    except DshApiError:
        return []

    return [row for row in rows if not _interrupted(dsh_session_history(row['session'], 2, base=base))]


def dsh_wait_run(owner: str, out_dir: str, timeout: float = 6 * 3600, *,
                 base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Дождаться ответов на все метки сессии `owner`: в файлы, метки снять.

    Returns:
        `[{session, title, out, brief}]` — забранные ответы; пусто — ждать было нечего
        или вышел срок (тогда метки остаются).
    """
    from dsh_.dsh_board import dsh_board_box_clear, dsh_board_brief, dsh_board_inbox_take  # ленивый: цикл импорта
    from dsh_.dsh_session import dsh_session_answer_last
    os.makedirs(out_dir, exist_ok=True)
    _waiter_replace(owner)
    with open(_waiter_path(owner), 'w', encoding='utf-8') as handle:
        handle.write(str(os.getpid()))
    taken, deadline = [], time.time() + timeout
    try:
        while dsh_wait_pending(owner) and time.time() < deadline:
            for row in dsh_wait_answered(dsh_wait_pending(owner), base=base):
                session = row['session']
                answer = dsh_session_answer_last(session, base=base) or ''
                path = os.path.join(out_dir, f'{session}.md')
                with open(path, 'w', encoding='utf-8', errors='replace') as out:
                    out.write(answer)
                dsh_board_inbox_take(session, row['turns'])  # снимает и метку
                dsh_board_box_clear(session, base=base)
                taken.append({'session': session, 'title': row['title'], 'out': path,
                              'brief': dsh_board_brief(answer)})
            if dsh_wait_pending(owner):
                time.sleep(DSH_WAIT_POLL)
    finally:
        # Файл чужого ждуна (сменившего этот через `_waiter_replace`) не трогаем.
        try:
            with open(_waiter_path(owner), encoding='utf-8') as handle:
                mine = handle.read().strip() == str(os.getpid())
            if mine:
                os.unlink(_waiter_path(owner))
        except OSError:
            pass
    return taken


def _waiter_replace(owner: str) -> None:
    """Погасить прежнего ждуна этой сессии: будит он только свой процесс Claude Code.

    Новый ждун той же сессии появляется, когда прежний пережил перезапуск харнеса (его
    будильник уже никуда не придёт) или когда ход запустил второй. Без замены они копились
    часами — по три-пять процессов на сессию. Гасим только процесс `dsh-board wait`:
    pid мог достаться чужому процессу.
    """
    try:
        with open(_waiter_path(owner), encoding='utf-8') as handle:
            pid = int(handle.read().strip())
        if pid == os.getpid():
            return
        with open(f'/proc/{pid}/cmdline', 'rb') as handle:
            cmdline = handle.read().replace(b'\0', b' ').decode('utf-8', 'replace')
        if 'dsh-board' in cmdline and ' wait' in cmdline:
            os.kill(pid, 15)
    except (OSError, ValueError):
        pass


def _interrupted(events: list) -> bool:
    ends = [event for event in events if event.get('type') == 'turn/end']
    reason = ((ends[-1].get('data') or {}).get('reason') or {}) if ends else {}
    return isinstance(reason, dict) and reason.get('kind') == 'interrupted'


def _key(session: str) -> str:
    return re.sub(r'-g\d+$', '', session or '')


def _home(name: str) -> str:
    return os.path.join(os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT), name)


def _waiter_path(owner: str) -> str:
    return _home(f"dsh-board.waiter.{re.sub(r'[^0-9a-zA-Z-]', '', owner)}.pid")


def _read() -> dict:
    try:
        with open(_home(DSH_WAIT_FILE), encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _edit(change) -> None:
    """Правка меток под файловым замком: пишут потоки fanout, ждун и чужие процессы."""
    path = _home(DSH_WAIT_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = _read()
        change(rows)
        # Просроченные метки никто уже не ждёт (`dsh_wait_marks` их пропускает) — не копим.
        edge = time.time() - DSH_WAIT_MAX_AGE
        for key in [key for key, row in rows.items()
                    if not isinstance(row, dict) or row.get('at', 0) <= edge]:
            rows.pop(key)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(rows, handle)
        os.replace(tmp, path)

"""«Ready»: сессия отработала, пока на неё не смотрели, — результат ждёт человека.

Сессия была «работает» (или с фоновой командой, или ждала ответа), закончила и стала
«ждёт», а человек с тех пор на неё не переходил — таблица пишет «Ready» зелёным, пока
не посмотрят. Посмотрели — это:
- клик по сессии в таблице (`claude_session_switch_to`);
- промпт человека в ней (хук `claude_hook_session_name`);
- её панель tmux на экране: активная панель активного окна в сессии tmux с клиентом.

⚠ Вкладку IDE не в tmux «на экране» не распознать: такая сессия остаётся «Ready», пока
по ней не кликнут или не напишут в неё. Своё окно статусная строка «Ready» не пишет.

Ключ — `sessionId`, у окна в фоновую сессию — и `job`; ещё `pid:<номер>`: после
`/resume` хуки получают id нового разговора, а таблица — прежний.
"""
import json
import os
import time

# Состояние: {ключ: {worked: когда последний раз работала, seen: когда смотрели}}.
CLAUDE_SESSION_READY_FILE = '~/.cache/claude-session-ready.json'

# Статусы «работает»: после них «ждёт» без взгляда человека — «Ready».
CLAUDE_SESSION_READY_WORK = ('busy', 'shell', 'waiting')


def claude_session_ready_mark(rows: list, shown: set) -> None:
    """Отметить работу и взгляды по строкам таблицы и поставить им `ready`.

    Args:
        rows: строки `claude_session_live_list` (нужны `sessionId`, `pid`, `status`, `pane`).
        shown: цели tmux (`session:window.pane`), которые сейчас на экране.
    """
    state = _claude_session_ready_read()
    now = time.time()
    for row in rows:
        keys = _claude_session_ready_keys(row.get('sessionId'), row.get('pid'), row.get('job'))
        for key in keys:
            item = state.setdefault(key, {})
            if row.get('status') in CLAUDE_SESSION_READY_WORK:
                item['worked'] = now
            if row.get('pane') and row['pane'] in shown:
                item['seen'] = now
        worked = max(state[key].get('worked', 0) for key in keys)
        seen = max(state[key].get('seen', 0) for key in keys)
        row['ready'] = row.get('status') == 'idle' and worked > seen
    _claude_session_ready_write(state)


def claude_session_ready_seen(session_id: str = '', pid: int = 0) -> None:
    """Человек посмотрел на сессию (кликнул, написал) — «Ready» снимается."""
    state = _claude_session_ready_read()
    for key in _claude_session_ready_keys(session_id, pid):
        state.setdefault(key, {})['seen'] = time.time()
    _claude_session_ready_write(state)


def _claude_session_ready_keys(session_id, pid, job='') -> list:
    return [key for key in (session_id or '', f'pid:{pid}' if pid else '', job or '') if key]


def _claude_session_ready_read() -> dict:
    try:
        with open(os.path.expanduser(CLAUDE_SESSION_READY_FILE), encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _claude_session_ready_write(state: dict) -> None:
    path = os.path.expanduser(CLAUDE_SESSION_READY_FILE)
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
    except OSError:
        pass

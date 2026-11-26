"""Закрыть сессию Claude Code из таблицы статусной строки — в два клика, без потери истории.

Первый клик по «✕» **взводит** закрытие (крестик краснеет), второй в течение
`CLAUDE_SESSION_CLOSE_WINDOW` секунд — закрывает; второго клика нет — взвод гаснет сам.
Крестик стоит вплотную к радиокнопке: одного промаха мимо неё не должно хватать.

Закрыть — значит завершить процесс, а не стереть: транскрипт остаётся, сессия
возвращается `claude --resume`. Сессия в tmux — закрывается её панель (`tmux_pane_kill`);
во вкладке IDE — `claude` получает SIGTERM, claude-shell выходит следом, вкладка остаётся
с оболочкой.
"""
import json
import os
import signal
import time

from claude_.session.claude_session_live import claude_session_live_find, claude_session_live_list
from tmux_.tmux_ import tmux_pane_kill

# Сколько секунд взвод ждёт второго клика.
CLAUDE_SESSION_CLOSE_WINDOW = 10

# Взведённые закрытия: {sessionId: когда взвели}.
CLAUDE_SESSION_CLOSE_ARMED = '~/.cache/claude-session-close.json'


def claude_session_close_click(session_id: str, hidden: bool = False) -> str:
    """Клик по «✕»: первый — взвести, второй в пределах окна — закрыть сессию.

    Args:
        hidden: «✕» скрытой строки (жива в tmux без вкладки) — закрываются **все скрытые**
            копии беседы (`session_id` — её id или id фоновой, в которую смотрят окна), а
            видимая копия той же беседы не трогается. ⚠ Без этого id двойника совпадает с
            видимой, и закрылась бы она. Взвод у скрытой строки свой — ключ `hidden:<id>`.

    Returns:
        «armed» — взведено, ждёт второго клика; «closed» — закрыта; иначе причина отказа.
    """
    if hidden:
        rows = [row for row in claude_session_live_list()
                if row.get('hidden') and session_id in (row['sessionId'], row['job'])]
        key = f'hidden:{session_id}'
    else:
        row = claude_session_live_find(session_id)
        rows = [row] if row else []
        key = row['sessionId'] if row else ''
    if not rows:
        return f'сессии {session_id} нет среди живых'
    armed = claude_session_close_armed()
    if key not in armed:
        armed[key] = time.time()
        _claude_session_close_save(armed)
        return 'armed'
    armed.pop(key, None)
    _claude_session_close_save(armed)
    if hidden:
        failed = [row['pane'] for row in rows if not tmux_pane_kill(row['pane'])]
        return f"tmux не закрыл панели {', '.join(failed)}" if failed else 'closed'
    row = rows[0]
    if row['pane']:
        return 'closed' if tmux_pane_kill(row['pane']) else f"tmux не закрыл панель {row['pane']}"
    try:
        os.kill(row['pid'], signal.SIGTERM)
    except OSError as error:
        return f"процесс {row['pid']} не остановлен: {error}"
    return 'closed'


def claude_session_close_armed() -> dict:
    """Взведённые и ещё не погасшие закрытия: `{sessionId: когда взвели}`."""
    try:
        with open(os.path.expanduser(CLAUDE_SESSION_CLOSE_ARMED), encoding='utf-8') as handle:
            armed = json.load(handle)
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {sid: at for sid, at in armed.items() if now - float(at) < CLAUDE_SESSION_CLOSE_WINDOW}


def _claude_session_close_save(armed: dict) -> None:
    path = os.path.expanduser(CLAUDE_SESSION_CLOSE_ARMED)
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(armed, handle)
        os.replace(tmp, path)
    except OSError:
        pass

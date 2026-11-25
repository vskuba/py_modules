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

from claude_.session.claude_session_live import claude_session_live_find
from tmux_.tmux_ import tmux_pane_kill

# Сколько секунд взвод ждёт второго клика.
CLAUDE_SESSION_CLOSE_WINDOW = 10

# Взведённые закрытия: {sessionId: когда взвели}.
CLAUDE_SESSION_CLOSE_ARMED = '~/.cache/claude-session-close.json'


def claude_session_close_click(session_id: str) -> str:
    """Клик по «✕»: первый — взвести, второй в пределах окна — закрыть сессию.

    Returns:
        «armed» — взведено, ждёт второго клика; «closed» — закрыта; иначе причина отказа.
    """
    row = claude_session_live_find(session_id)
    if not row:
        return f'сессии {session_id} нет среди живых'
    armed = claude_session_close_armed()
    if row['sessionId'] not in armed:
        armed[row['sessionId']] = time.time()
        _claude_session_close_save(armed)
        return 'armed'
    armed.pop(row['sessionId'], None)
    _claude_session_close_save(armed)
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

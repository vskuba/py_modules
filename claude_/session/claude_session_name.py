"""Своё название сессии Claude Code — заданное человеком, сильнее заголовка Claude Code.

Человек пишет в сессии `t Ссылки в статусной строке` — хук
`claude_hook_session_name` запоминает название здесь, и таблица сессий показывает его
вместо заголовка, который придумал Claude Code.

Хранится по двум ключам сразу: `sessionId` разговора и процесс `claude`. ⚠ Одного
`sessionId` мало: после `/resume` внутри сессии Claude Code отдаёт хукам id нового
разговора, а таблица знает процесс и прежний id из файла сессии.
"""
import json
import os
import time

# Названия: {ключ: {name, at}}; ключ — `sessionId` или `pid:<номер процесса>`.
CLAUDE_SESSION_NAME_FILE = '~/.cache/claude-session-names.json'


def claude_session_name_set(name: str, session_id: str = '', pid: int = 0) -> None:
    """Запомнить название сессии — по её `sessionId` и по процессу `claude`."""
    names = _claude_session_name_read()
    for key in (session_id, f'pid:{pid}' if pid else ''):
        if key:
            names[key] = {'name': name, 'at': time.time()}
    _claude_session_name_write(names)


def claude_session_name_get(*keys) -> str:
    """Название сессии по первому найденному ключу (`sessionId`, `pid:<номер>`); нет — пусто."""
    names = _claude_session_name_read()
    for key in keys:
        if key and isinstance(names.get(key), dict):
            return str(names[key].get('name') or '')
    return ''


def _claude_session_name_read() -> dict:
    try:
        with open(os.path.expanduser(CLAUDE_SESSION_NAME_FILE), encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _claude_session_name_write(names: dict) -> None:
    path = os.path.expanduser(CLAUDE_SESSION_NAME_FILE)
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(names, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass

"""Сколько токенов потратила сессия Claude Code — по её транскрипту, с дочитыванием.

Транскрипт находит `claude_session_transcript_path`; у каждого ответа модели
`message.usage`. Считается «потрачено»: вход без кэша + запись в кэш + выход.
Чтение из кэша (`cache_read_input_tokens`) не входит: оно повторяет весь контекст на
каждом шаге и за час набегает в десятки миллионов, ничего не говоря о работе.

⚠ Один ответ модели пишется в транскрипт несколькими строками (по блоку содержимого) с
одним `message.id` и одинаковым `usage` — без отбора по `id` счёт выходит в разы больше.
⚠ Транскрипты длинные (десятки МБ), а статусная строка зовёт счёт раз в секунду: файл
дочитывается с места прошлой остановки, состояние — в `CLAUDE_SESSION_TOKENS_CACHE`.
"""
import json
import os

from claude_.session.claude_session_transcript import claude_session_transcript_path

# Состояние дочитывания: {sessionId: {path, offset, total, ids_tail}}.
CLAUDE_SESSION_TOKENS_CACHE = '~/.cache/claude-session-tokens.json'

# Сколько последних id ответов помнить, чтобы не счесть хвост ответа дважды.
CLAUDE_SESSION_TOKENS_IDS = 64


def claude_session_tokens(sessions: list) -> dict:
    """Потраченные токены сессий Claude Code: `{sessionId: число}`.

    Args:
        sessions: `[{sessionId, cwd}]` — `cwd` нужен, чтобы найти транскрипт без обхода.

    Сессия без транскрипта — 0. Счёт дочитывается с прошлого места, общий кэш на диске.
    """
    cache = _claude_session_tokens_load()
    result = {}
    for row in sessions:
        sid = row.get('sessionId') or ''
        if not sid:
            continue
        state = cache.get(sid) or {}
        path = state.get('path') or claude_session_transcript_path(sid, row.get('cwd') or '')
        if not path:
            result[sid] = 0
            continue
        state = _claude_session_tokens_read(path, state)
        cache[sid] = state
        result[sid] = state['total']
    _claude_session_tokens_save(cache)
    return result


def _claude_session_tokens_read(path: str, state: dict) -> dict:
    offset, total = int(state.get('offset') or 0), int(state.get('total') or 0)
    seen = list(state.get('ids_tail') or [])
    try:
        size = os.path.getsize(path)
        if size < offset:  # файл переписан — считать заново
            offset, total, seen = 0, 0, []
        with open(path, 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return {'path': path, 'offset': offset, 'total': total, 'ids_tail': seen}
    end = chunk.rfind(b'\n') + 1  # недописанную строку оставить до следующего раза
    for line in chunk[:end].splitlines():
        if b'"usage"' not in line:
            continue
        try:
            message = json.loads(line).get('message') or {}
        except ValueError:
            continue
        usage, mid = message.get('usage') or {}, message.get('id')
        if not usage or (mid and mid in seen):
            continue
        if mid:
            seen = (seen + [mid])[-CLAUDE_SESSION_TOKENS_IDS:]
        total += sum(int(usage.get(key) or 0) for key in
                     ('input_tokens', 'cache_creation_input_tokens', 'output_tokens'))
    return {'path': path, 'offset': offset + end, 'total': total, 'ids_tail': seen}


def _claude_session_tokens_load() -> dict:
    try:
        with open(os.path.expanduser(CLAUDE_SESSION_TOKENS_CACHE), encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _claude_session_tokens_save(cache: dict) -> None:
    path = os.path.expanduser(CLAUDE_SESSION_TOKENS_CACHE)
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(cache, handle)
        os.replace(tmp, path)
    except OSError:
        pass

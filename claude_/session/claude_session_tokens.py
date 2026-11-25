"""Сводка сессии Claude Code по её транскрипту, с дочитыванием: токены и заголовок.

Транскрипт находит `claude_session_transcript_path`; у каждого ответа модели
`message.usage`. Считается раздельно: «вход» — вход без кэша + запись в кэш, «выход» —
что модель написала.
Чтение из кэша (`cache_read_input_tokens`) не входит: оно повторяет весь контекст на
каждом шаге и за час набегает в десятки миллионов, ничего не говоря о работе.

⚠ Один ответ модели пишется в транскрипт несколькими строками (по блоку содержимого) с
одним `message.id` и одинаковым `usage` — без отбора по `id` счёт выходит в разы больше.
Заголовок — то, что Claude Code ставит в шапку вкладки терминала: последняя запись
`custom-title` (сессию переименовал человек), иначе последняя `ai-title`.
⚠ Запись `custom-title` вживую не встречалась: имя поля (`customTitle` или `title`) —
по аналогии с `ai-title`/`aiTitle`, принимаются оба.

⚠ Транскрипты длинные (десятки МБ), а статусная строка зовёт счёт раз в секунду: файл
дочитывается с места прошлой остановки, состояние — в `CLAUDE_SESSION_TOKENS_CACHE`.
"""
import json
import os

from claude_.session.claude_session_transcript import claude_session_transcript_path

# Состояние дочитывания: {sessionId: {path, offset, tin, tout, ids_tail, ai, custom}}.
CLAUDE_SESSION_TOKENS_CACHE = '~/.cache/claude-session-tokens.json'

# Сколько последних id ответов помнить, чтобы не счесть хвост ответа дважды.
CLAUDE_SESSION_TOKENS_IDS = 64


def claude_session_tokens(sessions: list) -> dict:
    """Токены и заголовок сессий Claude Code: `{sessionId: {tokens_in, tokens_out, title}}`.

    Args:
        sessions: `[{sessionId, cwd}]` — `cwd` нужен, чтобы найти транскрипт без обхода.

    Сессия без транскрипта — нули и пустой заголовок. Дочитывается с прошлого места,
    общий кэш на диске.
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
            result[sid] = {'tokens_in': 0, 'tokens_out': 0, 'title': ''}
            continue
        state = _claude_session_tokens_read(path, state)
        cache[sid] = state
        result[sid] = {'tokens_in': state['tin'], 'tokens_out': state['tout'],
                       'title': state['custom'] or state['ai']}
    _claude_session_tokens_save(cache)
    return result


def _claude_session_tokens_read(path: str, state: dict) -> dict:
    offset, tin, tout = int(state.get('offset') or 0), int(state.get('tin') or 0), int(state.get('tout') or 0)
    seen = list(state.get('ids_tail') or [])
    titles = {'ai': state.get('ai') or '', 'custom': state.get('custom') or ''}
    try:
        size = os.path.getsize(path)
        # Файл переписан или запись кэша старше раздельного счёта — считать заново.
        if size < offset or 'tout' not in state:
            offset, tin, tout, seen, titles = 0, 0, 0, [], {'ai': '', 'custom': ''}
        with open(path, 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return {'path': path, 'offset': offset, 'tin': tin, 'tout': tout, 'ids_tail': seen, **titles}
    end = chunk.rfind(b'\n') + 1  # недописанную строку оставить до следующего раза
    for line in chunk[:end].splitlines():
        if b'"usage"' not in line and b'-title"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('type') == 'ai-title':
            titles['ai'] = entry.get('aiTitle') or titles['ai']
            continue
        if entry.get('type') == 'custom-title':
            titles['custom'] = entry.get('customTitle') or entry.get('title') or titles['custom']
            continue
        message = entry.get('message') or {}
        usage, mid = message.get('usage') or {}, message.get('id')
        if not usage or (mid and mid in seen):
            continue
        if mid:
            seen = (seen + [mid])[-CLAUDE_SESSION_TOKENS_IDS:]
        tin += int(usage.get('input_tokens') or 0) + int(usage.get('cache_creation_input_tokens') or 0)
        tout += int(usage.get('output_tokens') or 0)
    return {'path': path, 'offset': offset + end, 'tin': tin, 'tout': tout, 'ids_tail': seen, **titles}


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

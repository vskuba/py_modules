"""Сводка сессии Claude Code по её транскрипту, с дочитыванием: токены и заголовок.

Транскрипт находит `claude_session_transcript_path`; у каждого ответа модели
`message.usage`. Считается раздельно: «вход» — вход без кэша + запись в кэш, «выход» —
что модель написала. Вдобавок — взвешенная цена сессии (`units`, веса прайса
`DSH_SAVING_WEIGHTS`, с чтением кэша): из неё процент окна подписки, как у субагентов.
Чтение из кэша (`cache_read_input_tokens`) не входит: оно повторяет весь контекст на
каждом шаге и за час набегает в десятки миллионов, ничего не говоря о работе.

⚠ Один ответ модели пишется в транскрипт несколькими строками (по блоку содержимого) с
одним `message.id` и одинаковым `usage` — без отбора по `id` счёт выходит в разы больше.
Заголовок — то, что Claude Code ставит в шапку вкладки терминала: последняя запись
`custom-title` (сессию переименовал человек), иначе последняя `ai-title`.
⚠ Запись `custom-title` вживую не встречалась: имя поля (`customTitle` или `title`) —
по аналогии с `ai-title`/`aiTitle`, принимаются оба.

Вопрос — `AskUserQuestion`, на который ещё нет `tool_result` с его id: сессия ждёт ответа.
⚠ Ответы берутся только из строк разговора (`type: user`): служебные `api-request-blob`
несут те же `tool_result` строкой выше самого вопроса — с ними свежий вопрос выглядел бы
отвеченным (та же ловушка описана в claude-shell, `_transcript_tool_results`).
⚠ Запрос разрешения на инструмент в транскрипт не пишется, пока не отвечен, — его не видно.

⚠ Транскрипты длинные (десятки МБ), а статусная строка зовёт счёт раз в секунду: файл
дочитывается с места прошлой остановки, состояние — в `CLAUDE_SESSION_TOKENS_CACHE`.
"""
import json
import os

from claude_.session.claude_session_transcript import claude_session_transcript_path
from dsh_.dsh_saving import DSH_SAVING_WEIGHTS

# Состояние дочитывания: {sessionId: {path, offset, tin, tout, units, ids_tail, ai, custom, asks}}.
CLAUDE_SESSION_TOKENS_CACHE = '~/.cache/claude-session-tokens.json'

# Сколько последних id ответов помнить, чтобы не счесть хвост ответа дважды.
CLAUDE_SESSION_TOKENS_IDS = 64


def claude_session_tokens(sessions: list) -> dict:
    """Токены и заголовок сессий Claude Code: `{sessionId: {tokens_in, tokens_out, units, title}}`.

    `units` — взвешенная цена во входных токенах (с чтением кэша), для процента окна;
    `question` — текст вопроса `AskUserQuestion`, который ждёт ответа; нет — пусто.

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
            result[sid] = {'tokens_in': 0, 'tokens_out': 0, 'units': 0.0, 'title': '', 'question': ''}
            continue
        state = _claude_session_tokens_read(path, state)
        cache[sid] = state
        asks = list(state['asks'].values())
        result[sid] = {'tokens_in': state['tin'], 'tokens_out': state['tout'], 'units': state['units'],
                       'question': asks[-1] if asks else '',
                       'title': state['custom'] or state['ai']}
    _claude_session_tokens_save(cache)
    return result


def _claude_session_tokens_read(path: str, state: dict) -> dict:
    offset, tin, tout = int(state.get('offset') or 0), int(state.get('tin') or 0), int(state.get('tout') or 0)
    units = float(state.get('units') or 0)
    seen = list(state.get('ids_tail') or [])
    titles = {'ai': state.get('ai') or '', 'custom': state.get('custom') or ''}
    asks = dict(state.get('asks') or {})
    try:
        size = os.path.getsize(path)
        # Файл переписан или запись кэша старше раздельного счёта — считать заново.
        if size < offset or 'asks' not in state:
            offset, tin, tout, units, seen, titles = 0, 0, 0, 0.0, [], {'ai': '', 'custom': ''}
            asks = {}
        with open(path, 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return {'path': path, 'offset': offset, 'tin': tin, 'tout': tout, 'units': units,
                'ids_tail': seen, 'asks': asks, **titles}
    end = chunk.rfind(b'\n') + 1  # недописанную строку оставить до следующего раза
    for line in chunk[:end].splitlines():
        if (b'"usage"' not in line and b'-title"' not in line
                and b'AskUserQuestion' not in line and b'tool_result' not in line):
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
        # Вопрос — до отбора повторов: он в той же строке ответа, что и `usage`.
        _claude_session_tokens_asks(entry.get('type'), message.get('content'), asks)
        usage, mid = message.get('usage') or {}, message.get('id')
        if not usage or (mid and mid in seen):
            continue
        if mid:
            seen = (seen + [mid])[-CLAUDE_SESSION_TOKENS_IDS:]
        fresh, write = int(usage.get('input_tokens') or 0), int(usage.get('cache_creation_input_tokens') or 0)
        out, read = int(usage.get('output_tokens') or 0), int(usage.get('cache_read_input_tokens') or 0)
        tin, tout = tin + fresh + write, tout + out
        units += (fresh * DSH_SAVING_WEIGHTS['input'] + write * DSH_SAVING_WEIGHTS['cache_write']
                  + out * DSH_SAVING_WEIGHTS['output'] + read * DSH_SAVING_WEIGHTS['cache_read'])
    return {'path': path, 'offset': offset + end, 'tin': tin, 'tout': tout, 'units': units,
            'ids_tail': seen, 'asks': asks, **titles}


def _claude_session_tokens_asks(kind: str, content, asks: dict) -> None:
    # Вопрос модели — в ответе (`assistant`), ответ человека — в строке разговора (`user`);
    # прочие типы строк (служебные `api-request-blob`) не считаются вовсе.
    if kind not in ('assistant', 'user') or not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if kind == 'assistant' and block.get('type') == 'tool_use' and block.get('name') == 'AskUserQuestion':
            questions = (block.get('input') or {}).get('questions') or [{}]
            asks[block.get('id') or ''] = str(questions[0].get('question') or 'вопрос')
        elif kind == 'user' and block.get('type') == 'tool_result':
            asks.pop(block.get('tool_use_id') or '', None)


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

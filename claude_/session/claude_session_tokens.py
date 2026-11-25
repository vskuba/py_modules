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

⚠ Транскрипты длинные (десятки МБ), а статусная строка зовёт счёт раз в секунду: файл
дочитывается с места прошлой остановки, состояние — в `CLAUDE_SESSION_TOKENS_CACHE`.
"""
import json
import os
import time
from datetime import datetime

from claude_.session.claude_session_transcript import claude_session_transcript_path
from dsh_.dsh_saving import DSH_SAVING_WEIGHTS

# Состояние дочитывания: {sessionId: {path, offset, tin, tout, units, buckets, ids_tail, ai, custom}}.

# Корзины взвешенных токенов по времени записи: ширина и сколько хранить (окно подписки — 5 ч).
CLAUDE_SESSION_TOKENS_BUCKET = 600
CLAUDE_SESSION_TOKENS_KEEP = 6 * 3600
CLAUDE_SESSION_TOKENS_CACHE = '~/.cache/claude-session-tokens.json'

# Сколько последних id ответов помнить, чтобы не счесть хвост ответа дважды.
CLAUDE_SESSION_TOKENS_IDS = 64


def claude_session_tokens(sessions: list) -> dict:
    """Токены и заголовок сессий Claude Code: `{sessionId: {tokens_in, tokens_out, units, title}}`.

    `units` — взвешенная цена во входных токенах (с чтением кэша), за всю жизнь сессии;
    `buckets`, `buckets_in`, `buckets_out` — цена, вход и выход по 10-минутным корзинам
    (`{начало корзины: число}`, последние 6 ч) — для **текущего** 5-часового окна
    (`claude_session_tokens_since`).

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
            result[sid] = {'tokens_in': 0, 'tokens_out': 0, 'units': 0.0, 'buckets': {},
                           'buckets_in': {}, 'buckets_out': {}, 'title': ''}
            continue
        state = _claude_session_tokens_read(path, state)
        cache[sid] = state
        result[sid] = {'tokens_in': state['tin'], 'tokens_out': state['tout'], 'units': state['units'],
                       'buckets': state['buckets'], 'buckets_in': state['buckets_in'],
                       'buckets_out': state['buckets_out'],
                       'title': state['custom'] or state['ai']}
    _claude_session_tokens_save(cache)
    return result


def claude_session_tokens_since(buckets: dict, since: float) -> float:
    """Взвешенная цена сессии с момента `since` (начала окна подписки) — по корзинам сводки."""
    edge = since - since % CLAUDE_SESSION_TOKENS_BUCKET
    return sum(value for key, value in (buckets or {}).items() if int(key) >= edge)


def _claude_session_tokens_bucket(stamp) -> int:
    # Начало 10-минутной корзины по метке записи (ISO, `…Z`); метки нет — сейчас.
    try:
        moment = datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp()
    except ValueError:
        moment = time.time()
    return int(moment - moment % CLAUDE_SESSION_TOKENS_BUCKET)


def _claude_session_tokens_read(path: str, state: dict) -> dict:
    offset, tin, tout = int(state.get('offset') or 0), int(state.get('tin') or 0), int(state.get('tout') or 0)
    units = float(state.get('units') or 0)
    buckets = dict(state.get('buckets') or {})
    buckets_in, buckets_out = dict(state.get('buckets_in') or {}), dict(state.get('buckets_out') or {})
    seen = list(state.get('ids_tail') or [])
    titles = {'ai': state.get('ai') or '', 'custom': state.get('custom') or ''}
    try:
        size = os.path.getsize(path)
        # Файл переписан или запись кэша старше раздельного счёта — считать заново.
        if size < offset or 'buckets_out' not in state:
            offset, tin, tout, units, seen, titles = 0, 0, 0, 0.0, [], {'ai': '', 'custom': ''}
            buckets, buckets_in, buckets_out = {}, {}, {}
        with open(path, 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return {'path': path, 'offset': offset, 'tin': tin, 'tout': tout, 'units': units,
                'buckets': buckets, 'buckets_in': buckets_in, 'buckets_out': buckets_out,
                'ids_tail': seen, **titles}
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
        fresh, write = int(usage.get('input_tokens') or 0), int(usage.get('cache_creation_input_tokens') or 0)
        out, read = int(usage.get('output_tokens') or 0), int(usage.get('cache_read_input_tokens') or 0)
        tin, tout = tin + fresh + write, tout + out
        cost = (fresh * DSH_SAVING_WEIGHTS['input'] + write * DSH_SAVING_WEIGHTS['cache_write']
                + out * DSH_SAVING_WEIGHTS['output'] + read * DSH_SAVING_WEIGHTS['cache_read'])
        units += cost
        bucket = str(_claude_session_tokens_bucket(entry.get('timestamp')))
        buckets[bucket] = buckets.get(bucket, 0.0) + cost
        buckets_in[bucket] = buckets_in.get(bucket, 0) + fresh + write
        buckets_out[bucket] = buckets_out.get(bucket, 0) + out
    edge = time.time() - CLAUDE_SESSION_TOKENS_KEEP
    buckets, buckets_in, buckets_out = ({key: value for key, value in table.items() if int(key) >= edge}
                                        for table in (buckets, buckets_in, buckets_out))
    return {'path': path, 'offset': offset + end, 'tin': tin, 'tout': tout, 'units': units,
            'buckets': buckets, 'buckets_in': buckets_in, 'buckets_out': buckets_out,
            'ids_tail': seen, **titles}


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

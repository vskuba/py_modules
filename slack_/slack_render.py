"""
Человеческий разбор Slack-дампа (история канала/треда в JSON) в компактный текст для отчёта.

«list=2000+ сообщений не впихнуть в один tool_execute» и «в тикетах дамп логов
в открытом виде» — перед тем как отдать переписку человеку или в LLM-провайдер,
JSON-дамп `conversations.history`/`conversations.replies` (сырой текст ответов
api.slack.com/api/*, те же поля, что в браузерной консоли) режется в компактный
дайджест: дата-время в формате `YYYY-MM-DD HH:MM:SS UTC`, цитаты через `> `,
файлы/ссылки отдельной строкой, боты помечены `(бот)`, длинные тексты обрезаны
не более `max_len` символов — секреты в дамп не утекают (значения кук/токенов
в дампе маскирует `data_/data_mask.py`).
"""
import datetime
import json

# ── константы ──

SLACK_RENDER_MAX_TEXT = 1500  # «не впихнуть 2000+ сообщений в tool_execute»: обрезаем тело
SLACK_RENDER_TS_FORMAT = '%Y-%m-%d %H:%M:%S UTC'  # «формат YYYY-MM-DD HH:MM:SS UTC»
SLACK_RENDER_QUOTE = '> '  # «кавычка для цитат»
SLACK_RENDER_BOT_TAG = ' (бот)'

# ── публичный API модуля ──


def slack_render_ts(ts: 'str | float') -> str:
    """Метка времени Slack (секунды с epoch, дробные) → 'YYYY-MM-DD HH:MM:SS UTC'."""
    try:
        seconds = float(ts)
    except (TypeError, ValueError):
        return str(ts)
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime(
        SLACK_RENDER_TS_FORMAT)


def slack_render_history(json_text: 'str | bytes', max_len: int = SLACK_RENDER_MAX_TEXT) -> tuple:
    """Разобрать JSON-дампереписки в компактный текст: `[время UTC] автор: текст`.

    «Человеческий разбор + цитаты через `> `, время `YYYY-MM-DD HH:MM:SS UTC`».
    Цитата (message.ts -> text другого сообщения) уходит строкой с `> `, файлы —
    `file: <имя>`, боты — тег `(бот)`; `is_msg_merged`/`thread_ts` помечаются.

    Args:
        json_text: сырой JSON-дампа (`{"ok":true,"channel":"CRLD5SBL4","messages":[...]}`)
            либо голый список сообщений.
        max_len: обрезать тело сообщения, если оно длиннее `max_len` символов
            («…[обр.]»).

    Returns:
        (текст, None) либо (None, 'текст ошибки'); пустой список — ('', None).
    """
    messages, err = _slack_render_load(json_text)
    if messages is None:
        return None, err
    lines = []
    for msg in messages:
        if not isinstance(msg, dict):
            lines.append(f'(строка дампа без JSON-объекта: {str(msg)[:80]})')
            continue
        stamp = slack_render_ts(msg.get('ts'))
        who = _slack_render_author(msg)
        body = _slack_render_onespace(str(msg.get('text') or '').strip())
        tags = []
        if msg.get('bot_id') or msg.get('subtype') == 'bot_message' \
                or (isinstance(msg.get('icons'), dict) and msg['icons'].get('emoji')):
            tags.append(SLACK_RENDER_BOT_TAG)
        if msg.get('thread_ts') and msg.get('thread_ts') != msg.get('ts'):
            tags.append(f'(тред с {slack_render_ts(msg.get("thread_ts"))})')
        if msg.get('is_msg_merged'):
            tags.append('(склейка: слитый текст в thread-корне)')
        head = f'[{stamp}] {who}{"".join(tags)}: '
        line = head + (body if len(body) <= max_len else body[:max_len] + '…[обр.]')
        uniq = []
        for f in [x.get('name') or x.get('title') or 'вложение'
                  for x in msg.get('files') or [] if isinstance(x, dict)]:
            if f not in uniq:
                uniq.append(f)
        if uniq:
            line += ' file: ' + ', '.join(uniq)
        for att in msg.get('attachments') or []:
            if isinstance(att, dict):
                line += f' | вложение: ' + _slack_render_onespace(
                    str(att.get('title') or att.get('fallback') or att.get('text') or 'attach'))
        lines.append(line)
    return '\n'.join(lines), None


def slack_render_digest(json_text: 'str | bytes', max_len: int = SLACK_RENDER_MAX_TEXT) -> tuple:
    """Свернуть JSON-дампереписки в дайджест: статистика авторов + цитаты/файлы.

    «2000+ сообщений не впихнуть в tool_execute»: вместо полного разбора — счётчик
    сообщений по авторам, первая/последняя метка времени, катехизис цитат
    (`SLACK_RENDER_QUOTE`) и файлов; длинные тексты обрезаются до `max_len`.

    Args:
        json_text: JSON-дампереписки (см. slack_render_history).
        max_len: обрезка тел в цитатах дайджеста.

    Returns:
        (текст, None) либо (None, 'текст ошибки').
    """
    messages, err = _slack_render_load(json_text)
    if messages is None:
        return None, err
    by_author, first, last, quoted, files_n = {}, None, None, 0, 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        who = _slack_render_author(msg)
        by_author[who] = by_author.get(who, 0) + 1
        stamp = float(msg.get('ts') or 0)
        if stamp:
            first = stamp if first is None or stamp < first else first
            last = stamp if last is None or stamp > last else last
        if str(msg.get('text') or '').lstrip().startswith(SLACK_RENDER_QUOTE.strip()):
            quoted += 1
        files_n += len(msg.get('files') or [])
    lines = [f'сообщений: {len(messages)}']
    for who in sorted(by_author, key=by_author.get, reverse=True):
        lines.append(f'  {by_author[who]:>6}  {who}')
    if first is not None and last is not None:
        lines.append(f'период: {slack_render_ts(first)} .. {slack_render_ts(last)}')
    if quoted:
        lines.append(f'цитат («{SLACK_RENDER_QUOTE.strip()}» в начале): {quoted}')
    if files_n:
        lines.append(f'файлов: {files_n}')
    return '\n'.join(lines), None


# ── детали реализации ──


def _slack_render_load(json_text: 'str | bytes') -> tuple:
    """Разобрать дампереписки: (список сообщений, None) либо (None, 'текст ошибки').

    Дамп — «conversations.history»-объект {ok, channel, messages:[...]} либо голой
    список сообщений; ошибки разбора — первой позицией None, текстом во второй."""
    if not isinstance(json_text, (str, bytes)):
        return None, f'текст должен быть str|bytes, а {type(json_text).__name__}'
    src = json_text.decode('utf-8', 'ignore') if isinstance(json_text, bytes) else json_text
    try:
        data = json.loads(src)
    except ValueError:
        return None, f'не JSON (первые 60 символов: {src[:60]!r})'
    if isinstance(data, dict):
        for key in ('messages', 'events', 'threads'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return None, f'в дампе нет списков сообщений (ключей messages/events/threads нет)'
    if not isinstance(data, list):
        return None, 'дампереписки — не список сообщений и не объект с messages/events/threads'
    return data, None


def _slack_render_author(msg: dict) -> str:
    """Автор сообщения: real_name > display_name > user_profile>bot_id > user > id."""
    profile = msg.get('user_profile') if isinstance(msg.get('user_profile'), dict) else {}
    for key in ('real_name', 'display_name'):
        if profile.get(key):
            return str(profile[key])
    for key in ('username', 'name', 'user', 'user_team', 'bot_id', 'bot_profile'):
        if msg.get(key):
            return str(msg[key])
    return 'неизвестный автор'


def _slack_render_onespace(text: str) -> str:
    """Схлопнуть переносы строк: одно сообщение — одна строка дампа."""
    return ' '.join(text.split())


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('history', 'digest'), help='как разобрать дамп')
    _P.add_argument('--path', default='-', help="файл дампа либо '-' для stdin")
    _C = _P.parse_args()
    try:
        _src = sys.stdin.read() if _C.path == '-' else open(_C.path, 'r', encoding='utf-8').read()
    except OSError as err:
        print(f'ошибка: дамп {_C.path} не читается: {err}', file=sys.stderr)
        raise SystemExit(1) from None
    _fn = slack_render_history if _C.cmd == 'history' else slack_render_digest
    _text, _err = _fn(_src)
    if _text is None:
        print(f'ошибка: {_err}', file=sys.stderr)
        raise SystemExit(1)
    print(_text)
    raise SystemExit(0)

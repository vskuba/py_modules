"""
Сводка алертов Slack-канала: какой алерт, по чему, когда впервые и сколько раз повторился.

Родилась на разборе DLQ кассира (2026-11): время, когда сообщение легло в DLQ,
нашлось только по первому `FIRING` в истории канала, а всё, что после, —
суточные повторы Alertmanager того же самого состояния. Читать это глазами в
канале нельзя: один пост `[FIRING:2]` несёт два разных алерта (две очереди), у
elastalert текст лежит не в `text`, а в `attachments[].text`.

## Что здесь

* `slack_alerts_bot_token` — bot-токен из env-файла; протух — освежает по
  refresh-токену (`oauth.v2.access`) и пишет обе новые половины обратно в файл.
  ⚠ refresh-токен одноразовый: ответ всегда несёт новый, и он сохраняется
  раньше, чем используется access-токен.
* `slack_alerts_history` — `conversations.history` канала постранично (`next_cursor`).
* `slack_alerts_events` — посты → события: `[FIRING:N] Имя` разбирается на N
  алертов по блокам `*Alert:*`, предмет — очередь (`Queue: \\`x\\``) либо значения
  в обратных кавычках первой строки блока. У прочих ботов (elastalert) имя —
  заголовок вложения, предмет — `\\`…\\``-значения первой строки текста.
* `slack_alerts_summary` — группировка по (имя, предмет): первый и последний
  FIRING, число повторов, RESOLVED.

⚠ PII: в алертах elastalert бывают письма клиентов и PAN в `data:` запросов.
Модуль берёт только заголовки и значения в обратных кавычках первой строки —
тела сообщений наружу не попадают.

## Env-файл

    SLACK_BOT_TOKEN=xoxe.xoxb-…            # живёт ~12 ч (token rotation)
    SLACK_BOT_TOKEN_EXPIRES_AT=1794950000  # epoch конца жизни
    SLACK_REFRESH_TOKEN=xoxe-1-…           # одноразовый
    SLACK_CLIENT_ID=… / SLACK_CLIENT_SECRET=…
    SLACK_CHANNEL_ALERTS_CRITICAL=CRLD5SBL4

CLI:

    python -m slack_.slack_alerts --env-file /var/www/<проект>/.env.local \\
        --channel-var SLACK_CHANNEL_ALERTS_CRITICAL --days 14
    python -m slack_.slack_alerts --env-file … --channel CRLD5SBL4 --events   # лента событий
"""
import argparse
import datetime
import pathlib
import re
import time

from slack_.slack_ import slack_api_call

SLACK_ALERTS_API_BASE = 'https://slack.com/api'  # bot-токен: slack.com/api, не api.slack.com
SLACK_ALERTS_PAGE = 200
SLACK_ALERTS_REFRESH_MARGIN = 300  # освежать за 5 минут до конца жизни
SLACK_ALERTS_TITLE_RE = re.compile(r'^\[(FIRING|RESOLVED)(?::\d+)?\]\s*(\S+)')
SLACK_ALERTS_BLOCK_RE = re.compile(r'\*Alert:\*')
SLACK_ALERTS_QUEUE_RE = re.compile(r'Queue:\s*`([^`]+)`')
SLACK_ALERTS_TICK_RE = re.compile(r'`([^`]+)`')
SLACK_ALERTS_LINK_RE = re.compile(r'<[^>|]*\|([^>]*)>|<([^>]*)>')
SLACK_ALERTS_SUBJECT_MAX = 80


def slack_alerts_bot_token(env_file: str, now: float | None = None) -> str:
    """Живой bot-токен из env-файла; протух — refresh и запись обратно в файл.

    Args:
        env_file: путь к env-файлу с ключами SLACK_* (см. докстринг модуля).
        now: текущее epoch-время (для тестов); None — time.time().

    Returns:
        access-токен.
    """
    env = _env_read(env_file)
    now = time.time() if now is None else now
    expires = int(env.get('SLACK_BOT_TOKEN_EXPIRES_AT') or 0)
    if env.get('SLACK_BOT_TOKEN') and now < expires - SLACK_ALERTS_REFRESH_MARGIN:
        return env['SLACK_BOT_TOKEN']
    got, err = slack_api_call('oauth.v2.access', params={
        'client_id': env.get('SLACK_CLIENT_ID', ''),
        'client_secret': env.get('SLACK_CLIENT_SECRET', ''),
        'grant_type': 'refresh_token',
        'refresh_token': env.get('SLACK_REFRESH_TOKEN', ''),
    }, base=SLACK_ALERTS_API_BASE)
    if err or not isinstance(got, dict) or not got.get('ok'):
        raise RuntimeError(f"refresh bot-токена не удался: {err or (got or {}).get('error')}")
    _env_write(env_file, {
        'SLACK_BOT_TOKEN': got['access_token'],
        'SLACK_REFRESH_TOKEN': got['refresh_token'],
        'SLACK_BOT_TOKEN_EXPIRES_AT': str(int(now + int(got.get('expires_in') or 0))),
    })
    return got['access_token']


def slack_alerts_history(token: str, channel: str, oldest: float = 0, limit: int = 1000) -> list:
    """Сообщения канала (свежие сверху) постранично до `limit` штук.

    Args:
        token: bot-токен.
        channel: id канала (`CRLD5SBL4`).
        oldest: epoch-граница снизу; 0 — без границы.
        limit: максимум сообщений всего.
    """
    messages, cursor = [], ''
    while len(messages) < limit:
        params = {'channel': channel, 'limit': str(min(SLACK_ALERTS_PAGE, limit - len(messages)))}
        if oldest:
            params['oldest'] = f'{oldest:.6f}'
        if cursor:
            params['cursor'] = cursor
        got, err = slack_api_call('conversations.history', token, params, base=SLACK_ALERTS_API_BASE)
        if err or not isinstance(got, dict) or not got.get('ok'):
            raise RuntimeError(f"conversations.history: {err or (got or {}).get('error')}")
        messages.extend(got.get('messages') or [])
        cursor = (got.get('response_metadata') or {}).get('next_cursor') or ''
        if not cursor:
            break
    return messages


def slack_alerts_events(messages: list) -> list:
    """Посты канала → события [{'ts','state','name','subject'}] по времени вверх.

    `[FIRING:2] Имя` с двумя блоками `*Alert:*` даёт два события; state —
    FIRING/RESOLVED или '' (не Alertmanager). Берутся только посты ботов:
    реплики людей и служебные `channel_join` — не алерты, и их текст наружу не идёт.
    """
    events = []
    for msg in messages:
        if not (msg.get('bot_id') or msg.get('subtype') == 'bot_message'):
            continue
        ts = float(msg.get('ts') or 0)
        for att in msg.get('attachments') or [{'title': '', 'text': msg.get('text') or ''}]:
            events.extend(_events_of(ts, att.get('title') or att.get('fallback') or '',
                                     att.get('text') or ''))
    return sorted(events, key=lambda e: e['ts'])


def slack_alerts_summary(events: list) -> list:
    """События → [{'name','subject','first','last','fired','resolved','state'}], свежие сверху."""
    groups: dict = {}
    for ev in events:
        row = groups.setdefault((ev['name'], ev['subject']), {
            'name': ev['name'], 'subject': ev['subject'], 'first': ev['ts'], 'last': ev['ts'],
            'fired': 0, 'resolved': 0, 'state': ''})
        row['first'], row['last'] = min(row['first'], ev['ts']), max(row['last'], ev['ts'])
        if ev['state'] == 'RESOLVED':
            row['resolved'] += 1
        else:
            row['fired'] += 1
        row['state'] = ev['state']
    return sorted(groups.values(), key=lambda r: r['last'], reverse=True)


def slack_alerts_format_ts(ts: float) -> str:
    """epoch → 'MM-DD HH:MM' UTC."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%m-%d %H:%M')


def _events_of(ts: float, title: str, text: str) -> list:
    """Одно вложение → события: блоки `*Alert:*` у Alertmanager, иначе одно."""
    title = _plain(title).strip()
    head = SLACK_ALERTS_TITLE_RE.match(title)
    if head:
        state, name = head.group(1), head.group(2)
        blocks = SLACK_ALERTS_BLOCK_RE.split(text)[1:] or [text]
        return [{'ts': ts, 'state': state, 'name': name, 'subject': _subject(b)} for b in blocks]
    first = _plain(text).strip().splitlines()[:1]
    name = title or (first[0][:SLACK_ALERTS_SUBJECT_MAX] if first else '?')
    return [{'ts': ts, 'state': '', 'name': name, 'subject': _subject(text if title else '')}]


def _subject(block: str) -> str:
    """Предмет алерта: очередь, иначе `…`-значения первой непустой строки."""
    queue = SLACK_ALERTS_QUEUE_RE.search(block)
    if queue:
        return queue.group(1)
    lines = [line for line in _plain(block).splitlines() if line.strip()]
    ticks = SLACK_ALERTS_TICK_RE.findall(lines[0]) if lines else []
    return ' '.join(ticks)[:SLACK_ALERTS_SUBJECT_MAX]


def _plain(text: str) -> str:
    """Slack-разметку ссылок `<url|текст>` → текст, `<url>` → url."""
    return SLACK_ALERTS_LINK_RE.sub(lambda m: m.group(1) or m.group(2) or '', text or '')


def _env_read(env_file: str) -> dict:
    """KEY=VALUE env-файла в dict (комментарии и пустые строки пропускаются)."""
    out = {}
    for line in pathlib.Path(env_file).read_text().splitlines():
        m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _env_write(env_file: str, values: dict) -> None:
    """Заменить значения ключей в env-файле на месте; отсутствующие дописать в конец."""
    path = pathlib.Path(env_file)
    text = path.read_text()
    for key, value in values.items():
        text, n = re.subn(rf'^{key}=.*$', lambda _m: f'{key}={value}', text, count=1, flags=re.M)
        if not n:
            text = text.rstrip('\n') + f'\n{key}={value}\n'
    path.write_text(text)


def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--env-file', required=True)
    ap.add_argument('--channel', default='', help='id канала')
    ap.add_argument('--channel-var', default='SLACK_CHANNEL_ALERTS_CRITICAL',
                    help='ключ env-файла с id канала (если --channel пуст)')
    ap.add_argument('--days', type=float, default=14)
    ap.add_argument('--limit', type=int, default=1000)
    ap.add_argument('--events', action='store_true', help='ленту событий вместо сводки')
    args = ap.parse_args()
    channel = args.channel or _env_read(args.env_file).get(args.channel_var, '')
    if not channel:
        raise SystemExit(f'нет id канала: --channel или {args.channel_var} в {args.env_file}')
    try:
        token = slack_alerts_bot_token(args.env_file)
        messages = slack_alerts_history(token, channel, time.time() - args.days * 86400, args.limit)
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')
    events = slack_alerts_events(messages)
    if args.events:
        for ev in events:
            print(f"{slack_alerts_format_ts(ev['ts'])}  {ev['state'] or '-':<8} "
                  f"{ev['name']:<50} {ev['subject']}")
        return
    for row in slack_alerts_summary(events):
        print(f"{slack_alerts_format_ts(row['first'])} .. {slack_alerts_format_ts(row['last'])}  "
              f"fired={row['fired']:<3} resolved={row['resolved']:<3} {row['state'] or '-':<8} "
              f"{row['name']:<50} {row['subject']}")


if __name__ == '__main__':
    _main()

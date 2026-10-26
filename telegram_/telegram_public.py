"""Публичные превью telegram: каналы и их лента без токена и аккаунта.

Публичный канал отдаёт ленту обычным HTTP — страница `t.me/s/<channel>`:
посты с точными датами, ссылки, упоминания, счётчик подписчиков и служебная
плашка «Channel created» с датой заведения. Для разведки этого хватает:
бот-токена нет, аккаунт не светится, а канал конкурента/подрядчика читается
как обычный сайт. Страница вьёт ~20 постов с пагинацией `?before=`; руками
разбирать её доводилось не раз — здесь тот же разбор одним вызовом.

## Что здесь есть

* `telegram_public_info` — шапка канала: название, описание, подписчики,
  дата заведения (если плашка есть на странице);
* `telegram_public_posts` — лента: посты с датой, текстом, ссылками и
  упоминаниями; пагинация в прошлое (`?before=`).

⚠ Приватные каналы превью не отдают; `t.me/xxx` для них вернёт заглушку.
⚠ Плашка «Channel created» есть не у всех каналов — пусто не значит, что
канал не датирован, просто страница её не показывает.
"""
import html as _html
import re
from datetime import datetime, timezone

import httpx

TG_S = 'https://t.me/s/'
_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'


def telegram_public_info(channel: str, *, timeout: float = 20.0) -> dict:
    """Шапка публичного канала с его же превью-страницы.

    Args:
        channel: имя канала (`durov`) или ссылка целиком.
        timeout: секунд на запрос.

    Returns:
        {'ok': bool, 'channel': …, 'title': '…', 'description': '…',
         'subscribers': int|None, 'created': 'ГГГГ-ММ-ДД'|'', 'error': …}
    """
    page, err = _telegram_public_page(channel, timeout=timeout)
    if page is None:
        return {'ok': False, 'channel': channel, 'title': '', 'description': '',
                'subscribers': None, 'created': '', 'error': err}
    title = _telegram_public_meta(page, 'og:title')
    desc = _telegram_public_meta(page, 'og:description')
    subs = re.search(r'counter_value">([\d\s\u2009,]+)</span>\s*'
                     r'<span class="counter_type">[^<]*(subscriber|member)',
                     page, re.S | re.I)
    born = re.search(r'class="tgme_widget_message_service_text[^>]*>[^<]*'
                     r'(\w+ \d{1,2}, \d{4}|Created|создан)', page, re.I)
    return {'ok': True, 'channel': channel, 'title': _telegram_public_clean(title),
            'description': _telegram_public_clean(desc),
            'subscribers': int(subs.group(1).replace(' ', '').replace('\u2009', '')) if subs else None,
            'created': born.group(1) if born else '', 'error': ''}


def telegram_public_posts(channel: str, *, pages: int = 1, before: int = 0,
                          timeout: float = 20.0) -> dict:
    """Лента публичного канала: посты в прошлое от свежих к старым.

    Args:
        channel: имя канала или ссылка.
        pages: сколько страниц по ~20 постов пройти (осторожно, т. — это их сервер).
        before: id, от которого листать (взять 'next' из прошлого ответа).
        timeout: секунд на страницу.

    Returns:
        {'ok': bool, 'channel': …, 'posts': [{'post','ts','text','links',
         'mentions'}, …], 'next': int|None, 'error': …}
    """
    posts, next_id, err = [], None, ''
    cur, page = before, None
    for n in range(pages):
        page, err = _telegram_public_page(channel, before=cur, timeout=timeout)
        if page is None:
            break
        for m in re.finditer(r'data-post="([^"]+)"[^>]*>.*?'
                             r'datetime="([^"]+)".*?'
                             r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)'
                             r'</div>', page, re.S):
            raw = m.group(3)
            links = re.findall(r'href="(https?://[^"]+)', raw)
            text = _telegram_public_clean(raw)
            posts.append({'post': m.group(1),
                          'ts': _telegram_public_ts(m.group(2)),
                          'text': text, 'links': links,
                          'mentions': re.findall(r'@[\w_]{4,}', text)})
        older = re.search(r'\?before=(\d+)', page)
        if not older or int(older.group(1)) == cur:
            next_id = None
            break
        cur = int(older.group(1))
        next_id = cur
    return {'ok': bool(posts), 'channel': channel, 'posts': posts,
            'next': next_id, 'error': err if not posts else ''}


def _telegram_public_page(channel: str, *, before: int = 0,
                         timeout: float = 20.0):
    """Одна страница превью; (html|None, ошибка)."""
    name = channel.rstrip('/').split('/')[-1].lstrip('@')
    try:
        r = httpx.get(f'{TG_S}{name}', params={'before': before} if before else None,
                      timeout=timeout, follow_redirects=True,
                      headers={'User-Agent': _UA})
    except httpx.HTTPError as e:
        return None, f'{type(e).__name__}'
    if r.status_code != 200 or 'tgme_widget_message' not in r.text and 'tgme_channel_info' not in r.text:
        return None, f'HTTP {r.status_code} или приватный'
    return r.text, ''


def _telegram_public_meta(page: str, prop: str) -> str:
    m = re.search(rf'property="{prop}"[^>]*content="([^"]*)"', page)
    return m.group(1) if m else ''


def _telegram_public_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + 'Z'
    except ValueError:
        return iso


def _telegram_public_clean(raw: str) -> str:
    """HTML → текст: ссылки инлайном, `<br>` переводы строки, сущности раскрыты."""
    raw = re.sub(r'<br\s*/?>', '\n', raw)
    raw = re.sub(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                 lambda m: m.group(2).strip() if m.group(2).strip() in m.group(1) else f'{m.group(2).strip()} {m.group(1)}',
                 raw, flags=re.S)
    text = _html.unescape(re.sub(r'<[^>]+>', '', raw))
    return re.sub(r'[ \t\u00a0]+', ' ', text).strip()


if __name__ == '__main__':
    import argparse
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('channel')
    _P.add_argument('--pages', type=int, default=1)
    _a = _P.parse_args()
    info = telegram_public_info(_a.channel)
    print('канал:', info['title'], '| подписчики:', info['subscribers'],
          '| создан:', info['created'], info['error'])
    feed = telegram_public_posts(_a.channel, pages=_a.pages)
    for p in feed['posts']:
        print(f"\n[{p['ts']}] {p['post']}\n{p['text'][:400]}")
        for u in p['links']:
            print('  →', u)

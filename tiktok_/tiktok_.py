"""
TikTok без входа: существует ли ручка, карточка профиля, поиск людей.

TikTok отдаёт анониму ровно три вещи, и этот модуль умеет все три:

* **существует ли ручка** — endpoint oembed: `200` с `author_name` — профиль
  жив, `400` — такой ручки нет. Ни браузера, ни капчи, один запрос на проверку,
  так что пачку из двадцати гипотез (`liudmyla_aqua`, `aqua_tech_ch`, …)
  прогоняет за десять секунд;
* **карточка профиля** — страница `/@ручка` через контейнер-браузер: имя,
  био, счётчики (подписчики/подписки/ролики). Прямые переходы по `/@…`
  TikTok иногда разворачивает на слайдер-капчу — переживает её только тот,
  кто пришёл из поиска, поэтому `tiktok_profile` идёт обход: сначала
  `search/user?q=ручка` (поиск капчу не ловит), и только по его результатам
  открывается сам профиль;
* **поиск по имени/городу** — `search/user?q=`: ссылки `/@ручка` с страницы,
  там же имена и счётчики. Это ответ на «ищи человека по имени и городу»
  без входа; выдача ранжируется не по точности, так что полный список —
  кандидаты, а не вердикт.

Сессия контейнера заводится на каждый вызов и гасится в конце; `proxy` —
когда адрес своего хоста TikTok уже знает в лицо.

Родственное: `insta_` (то же про Instagram), `browser_http` (транспорт).
"""

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from browser_.browser_http import (browser_http_close, browser_http_goto,
                                   browser_http_open, browser_http_wait)

TIKTOK_URL = 'https://www.tiktok.com'
TIKTOK_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/124 Safari/537.36')
# Поиск дорисовывается на глазу: 'Followers' появляется, когда список собран.
TIKTOK_WAIT = 'Followers'

# Строка кандидата в тексте страницы: аватар с alt, имя, ручка, счётчики.
# Ссылок с href карточка не даёт (клик вешается на обработчик), — читают текст.
TIKTOK_ROW = re.compile(
    r'!\[Avatar for ([^\]]+)\]\n([^\n]*)\n([^\n]*)\n([\d.,KM]+)\nFollowers'
    r'(?:\s*·\s*\n([\d.,KM]+)\nLikes)?')


def tiktok_exists(handles: list) -> dict:
    """Живы ли ручки: `{ручка: True/False}` через oembed, по одному запросу.

    400 — ручки нет; любой другой ответ (461, таймаут) — не «нет», а «не
    спросили»: такие ручки в словаре нет, отличая отсутствие от неизвестности.
    """
    out = {}
    for h in handles:
        req = urllib.request.Request(
            f'{TIKTOK_URL}/oembed?url=' + urllib.parse.quote(f'{TIKTOK_URL}/@{h}', safe=''),
            headers={'User-Agent': TIKTOK_UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                out[h] = bool(json.loads(r.read()).get('author_name'))
        except urllib.error.HTTPError as e:
            if e.code == 400:
                out[h] = False
        except Exception:
            pass

    return out


def tiktok_search(query: str, proxy: str = '', browser: str = '') -> list:
    """Кандидаты по имени/городу: `[{handle, text}]` со страницы `search/user`.

    Строка карточки читается из текста (ссылок с href страница не даёт):
    `{handle, name, followers, likes}`. Порода выдачи шумная: близко сидящие
    по буквам имена всплывают выше точного совпадения — полный список это
    кандидаты, а не вердикт.
    """
    sid = browser_http_open('tiktok-search', proxy, browser)
    try:
        browser_http_goto(sid, f'{TIKTOK_URL}/search/user?q='
                          + urllib.parse.quote(query), 'load', 60000)
        out, seen = [], set()
        for m in TIKTOK_ROW.finditer(browser_http_wait(sid, TIKTOK_WAIT, tries=8).get('text') or ''):
            h = m[1].strip()
            if h.lower() not in seen:
                seen.add(h.lower())
                out.append({'handle': h, 'name': m[2].strip(),
                            'followers': _num(m[4]), 'likes': _num(m[5] or '')})

        return out
    finally:
        browser_http_close(sid)


def tiktok_profile(handle: str, proxy: str = '', browser: str = '') -> dict:
    """Карточка профиля: `{handle, found, name, bio, followers, following, videos}`.

    Идёт прямиком на `/@ручка`; если адрес её ловит на слайдер-капчу (бывает
    на адресе, который TikTok уже знает), второй заход через `search/user?q=`
    — поиск капчу не раздаёт. `following` — «в подписках»: для OSINT поле
    часто ценнее подписчиков (локальный граф человека).
    """
    sid = browser_http_open('tiktok-profile', proxy, browser)
    try:
        browser_http_goto(sid, f'{TIKTOK_URL}/@{handle}', 'domcontentloaded', 60000)
        text = browser_http_wait(sid, 'Followers', tries=8).get('text') or ''
        if 'Followers' not in text and not _search_hit(sid, handle):
            return {'handle': handle, 'found': False}

        return {'handle': handle, 'found': True, **_profile_fields(text)}
    finally:
        browser_http_close(sid)


def _search_hit(sid: str, handle: str) -> bool:
    """Тот же адрес, но из поиска: капча пропускает пришедших с `search/user`."""
    browser_http_goto(sid, f'{TIKTOK_URL}/search/user?q=' + urllib.parse.quote(handle),
                      'load', 60000)
    browser_http_wait(sid, TIKTOK_WAIT, tries=8)
    browser_http_goto(sid, f'{TIKTOK_URL}/@{handle}', 'domcontentloaded', 60000)

    return 'Followers' in (browser_http_wait(sid, 'Followers', tries=8).get('text') or '')


def _profile_fields(text: str) -> dict:
    """Поля со страницы профиля: имена в `##`-заголовках, счётчики слиты с числами."""
    heads = [l[3:].strip() for l in text.splitlines() if l.startswith('## ')]

    return {'name': heads[0] if heads else '', 'bio': heads[1] if len(heads) > 1 else '',
            'followers': _num(text, r'([\d.,KM]+)\s*Followers'),
            'following': _num(text, r'([\d.,KM]+)\s*Following'),
            'videos': 0 if 'not published' in text.lower() else _num(text, r'([\d.,KM]+)\s*videos')}


def tiktok_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='tiktok без входа')
    sub = parser.add_subparsers(dest='command', required=True)

    ex = sub.add_parser('exists', help='живы ли ручки (oembed, без браузера)')
    ex.add_argument('handles', nargs='+')

    se = sub.add_parser('search', help='кандидаты по имени/городу')
    se.add_argument('query')
    se.add_argument('--proxy', default='')
    se.add_argument('--browser', default='', help='адрес контейнера, если не из .env')

    pr = sub.add_parser('profile', help='карточка профиля')
    pr.add_argument('handle')
    pr.add_argument('--proxy', default='')
    pr.add_argument('--browser', default='')

    args = parser.parse_args(argv)
    if args.command == 'exists':
        for h, ok in tiktok_exists(args.handles).items():
            print(f'{h:30} {"жив" if ok else "нет"}')
    elif args.command == 'search':
        for r in tiktok_search(args.query, args.proxy, args.browser):
            print(f"@{r['handle']:25} {r['name']:28} {r['followers']:>8} подп.")
    else:
        print(json.dumps(tiktok_profile(args.handle, args.proxy, args.browser),
                         ensure_ascii=False, indent=1))

    return 0




def _num(text: str, pattern: str = '') -> int:
    """Число из «24.5K» или из строки по шаблону; TikTok пишет K и M."""
    raw = (re.search(pattern, text, re.I)[1] if pattern and re.search(pattern, text, re.I)
           else text).replace(',', '').replace('.', '').strip().upper()
    if not raw:
        return 0
    for suf, mult in (('M', 1_000_000), ('K', 1_000)):
        if raw.endswith(suf):
            return int(float(raw[:-1]) * mult)

    return int(raw)


if __name__ == '__main__':
    raise SystemExit(tiktok_main())

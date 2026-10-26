"""Ключи наружу: что страница отдаёт миру в скриптах — пиксели, капчи, карты, платежи.

Витрины сети интересны не только вывеской: в их JS лежат sitekey капчи, ключи
карт, id пикселей, формы Tilda и реквизиты платёжных шлюзов. Один и тот же
formskey кормит два корня, один пиксель — две фермы: ключ связывает то, что
разъехалось по доменам. Собирать это руками — охотиться за регулярками каждый
заход заново; здесь — реестр масок и один вызов на страницу.

## Что здесь есть

* `osint_keys_harvest` — страница (и, по желанию, её внешние скрипты) → словарь
  найденных ключей по типам;
* `osint_keys_sweep` — пачке URL та же охота, строкой на каждый.

⚠⚠ Находка — не публикация: часть id (GTM, AdRiver, телефонные номера) по
договорённости в отчёты не выносится; решение, что публиковать, остаётся за
аналитиком — инструмент только находит.
⚠ Ключи из минифицированных бандлов могут дублироваться в нескольких формах —
типы схлопнуты в множества, порядок внутри не показан.
"""
import argparse
import re
from urllib.parse import urljoin

import httpx

OSINT_KEYS_UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120'}
# Внешних скриптов на страницу в deep-режиме: больше — уже не витрина, а портал.
OSINT_KEYS_DEEP_MAX = 12
OSINT_KEYS_SRC = re.compile(r'<script[^>]*\bsrc\s*=\s*["\']?([^"\'\s>]+)', re.I)

# Тип → маски; первая непустая группа — значение, без групп — вся находка.
OSINT_KEYS = {
    'recaptcha': (r'sitekey["\']?\s*[=:]\s*["\']([A-Za-z0-9_-]{20,})',
                  r'recaptcha/api\.js\?render=([A-Za-z0-9_-]{20,})',
                  r'grecaptcha\.execute\(["\']([A-Za-z0-9_-]{20,})'),
    'maps': (r'(AIza[0-9A-Za-z_-]{35})',),
    'yandex_map': (r'apikey=([A-Za-z0-9_-]{10,})',),
    'gtm': (r'(GTM-[A-Z0-9]{4,10})',),
    'ga4': (r'(G-[A-Z0-9]{6,12})',),
    'ua': (r'(UA-\d{6,10}(?:-\d{1,2})?)',),
    'fb_pixel': (r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,})['\"]",
                 r'connect\.facebook\.net/en_US/fbevents\.js'),
    'clarity': (r"clarity\(\s*['\"]set['\"]\s*,\s*['\"]([a-z0-9]{6,12})['\"]",
                r'clarity\.ms/([a-z0-9]{6,12})/js'),
    'tiktok': (r"ttq\.load\(\s*['\"]([A-Za-z0-9]{6,})['\"]",
               r"pixelId['\"]?\s*[=:]\s*['\"]?(\d{10,})"),
    'hotjar': (r'static\.hotjar\.com/\d+/(\d{4,7})',),
    'tilda_forms': (r'forms\.tilda\.cc/forms/([a-z0-9-]{6,48})/submit',),
    'gateway': (r'((?:auth|pay)\.pay\.ua|api\.payport\.ua|secure\.wayforpay\.com'
                r'|api\.fondy\.ua|fund\.raiffeisen\.ru|payment\.privatbank\.ua'
                r'|pay\.lionbridge\.com)',),
    'merchant': (r'(?:merchant|shopId|storeName)["\']?\s*[=:]\s*["\']'
                 r'([A-Za-z0-9._-]{4,48})',),
    'yandex_metrica': (r'(?:mc\.yandex\.ru/metrika/(?:watch|tag)\.js'
                       r'|ym\(\s*["\']?(\d{4,10}))',),
    'sentry': (r'(?:sentry\.io/[^"\']*?[?&]key=|DSN["\']?\s*[=:]\s*["\']?'
               r'https://([a-z0-9]{20,})@[a-z.]+/[0-9]+)',),
}


def osint_keys_harvest(url: str, *, deep: bool = False,
                       timeout: float = 20.0) -> dict:
    """Одна страница: ключи из разметки и, если `deep`, из её внешних скриптов.

    Args:
        url: адрес страницы.
        deep: подтянуть внешние `<script src>` (до `OSINT_KEYS_DEEP_MAX`) и
            искать и в них тоже.
        timeout: секунд на запрос.

    Returns:
        {'ok': хоть что-то, 'url': …, 'found': {тип: [значения]},
         'sources': [{url, kinds: [типы]}], 'error': словами}
    """
    found: dict[str, list] = {}
    sources = []

    def hunt(where: str, body: str):
        got = []
        for kind, masks in OSINT_KEYS.items():
            for mask in masks:
                for m in re.finditer(mask, body):
                    val = next((g for g in m.groups() if g), m.group(0))
                    if val not in found.setdefault(kind, []):
                        found[kind].append(val)
                        got.append(kind)
        if got:
            sources.append({'url': where, 'kinds': sorted(set(got))})

    try:
        r = httpx.get(url, headers=OSINT_KEYS_UA, follow_redirects=True,
                      timeout=timeout)
    except httpx.HTTPError as e:
        return {'ok': False, 'url': url, 'found': {}, 'sources': [],
                'error': f'{type(e).__name__}'}
    hunt(url, r.text)
    if deep:
        for src in list(dict.fromkeys(
                OSINT_KEYS_SRC.findall(r.text)))[:OSINT_KEYS_DEEP_MAX]:
            where = urljoin(str(r.url), src)
            try:
                body = httpx.get(where, headers=OSINT_KEYS_UA,
                                 follow_redirects=True, timeout=timeout).text
            except httpx.HTTPError:
                continue
            hunt(where, body)
    return {'ok': bool(found), 'url': url,
            'found': {k: v for k, v in found.items() if v},
            'sources': sources, 'error': ''}


def osint_keys_sweep(urls: list, *, deep: bool = False,
                     timeout: float = 20.0) -> dict:
    """Пачке URL — охота по очереди; строка на каждый, банов не наживаем.

    Args:
        urls: адреса страниц.
        deep: см. `osint_keys_harvest`.
        timeout: секунд на запрос.

    Returns:
        {'ok': хоть где-то хоть что-то, 'rows': [{url, found, error}]}
    """
    rows = []
    for u in urls:
        res = osint_keys_harvest(u, deep=deep, timeout=timeout)
        rows.append({'url': res['url'], 'found': res['found'],
                     'error': res['error']})
    return {'ok': any(r['found'] for r in rows), 'rows': rows}


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('url')
    _P.add_argument('--deep', action='store_true',
                    help='заглянуть во внешние скрипты')
    _a = _P.parse_args()
    _res = osint_keys_harvest(_a.url, deep=_a.deep)
    for _k, _vals in sorted(_res['found'].items()):
        for _v in _vals:
            print(f'{_k:<14} {_v}')
    for _s in _res['sources']:
        print('#', _s['url'], '→', ','.join(_s['kinds']))
    if _res['error']:
        print('не вышло:', _res['error'])
    raise SystemExit(0 if _res['ok'] else 1)

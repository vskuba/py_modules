"""Ассортимент магазина через sitemap: что он продаёт, когда витрина закрыта.

`/products.json` у Shopify скрипту — 403, но `sitemap_products_1.xml` открыт
всем: внутри `<image:title>` с названиями товаров и `<loc>` с ручками; то же у
других движков — sitemap магазин сам строит для краулеров и за CDN не прячет.
Для сверки вывесок (тот же «Ariete»: чей товар на чужом домене — очки или
чайники) это единственный дешёвый способ увидеть ассортимент целиком, не
листая витрину. Здесь — платформа, список sitemap'ов, счётчики и выборка
названий; номера моделей — отдельной функцией из названий.

## Что здесь есть

* `osint_shop_map` — платформа, sitemap'ы, число товаров, выборка названий и
  коллекций;
* `osint_shop_models` — номера-«артикулы» из списка названий, счётчиком.

⚠ Sitemap ≠ каталог: из него выпадает снятое с индекса, а не с сайта; молчать
он может и при живой витрине.
⚠ Платформа угадывается по маркерам в разметке — самописный шаблон на чужом
движке выдаст чужой движок; сверяйте с куками и фронтом.
"""
import argparse
import re
from urllib.parse import urljoin, urlparse

import httpx

OSINT_SHOP_UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120'}
OSINT_SHOP_PLATFORM = {
    'shopify': ('cdn.shopify.com', 'myshopify', 'Shopify.shop'),
    'opencart': ('index.php?route=', 'cart/information'),
    'woocommerce': ('wp-content/plugins/woocommerce', 'woocommerce'),
    'prestashop': ('prestashop', 'module/'),
    'okaycms': ('OkayCMS', 'catalog/view'),
    'tilda': ('tilda.cc', 'tildastat'),
}
OSINT_SHOP_SITEMAP = re.compile(r'^\s*(?:Sitemap|sitemap)\s*:\s*(\S+)', re.M)
OSINT_SHOP_LOC = re.compile(r'<loc>\s*([^<\s]+)\s*</loc>')
OSINT_SHOP_TITLE = re.compile(r'<image:title>\s*(?:<!\[CDATA\[)?(.*?)'
                              r'(?:\]\]>)?\s*</image:title>', re.S)
# Номер-«артикул»: 3–6 цифр, факультативный хвост через дефис/косую.
OSINT_SHOP_MODEL = re.compile(r'\b\d{3,6}(?:[-/]\d{2,4})?\b')


def osint_shop_map(url: str, *, timeout: float = 20.0,
                   pages: int = 6) -> dict:
    """Карта ассортимента: платформа, sitemap'ы, число товаров, выборки.

    Args:
        url: любой адрес магазина (схема нужна; голое имя — http://).
        timeout: секунд на запрос.
        pages: максимум product-sitemap'ов (у Shopify они постраничные).

    Returns:
        {'ok': хоть что-то, 'domain': …, 'platform': '…'| '',
         'sitemaps': {'products': […], 'collections': […], 'other': […]},
         'products': int, 'titles': [до 80], 'handles': [до 40],
         'collections': [до 60], 'error': словами}
    """
    if '://' not in url:
        url = 'https://' + url
    root = '%s://%s' % urlparse(url)[:2]
    out = {'ok': False, 'domain': urlparse(url).netloc, 'platform': '',
           'sitemaps': {'products': [], 'collections': [], 'other': []},
           'products': 0, 'titles': [], 'handles': [], 'collections': [],
           'error': ''}
    try:
        page = httpx.get(url, headers=OSINT_SHOP_UA, follow_redirects=True,
                         timeout=timeout).text
        robots = httpx.get(urljoin(root, '/robots.txt'), headers=OSINT_SHOP_UA,
                           follow_redirects=True, timeout=timeout).text
    except httpx.HTTPError as e:
        out['error'] = f'{type(e).__name__}'
        return out
    for name, marks in OSINT_SHOP_PLATFORM.items():
        if any(m in page for m in marks):
            out['platform'] = name
            break
    maps = OSINT_SHOP_SITEMAP.findall(robots) or [urljoin(root, '/sitemap.xml')]
    for m in maps:
        try:
            body = httpx.get(m, headers=OSINT_SHOP_UA, follow_redirects=True,
                             timeout=timeout).text
        except httpx.HTTPError:
            continue
        kids = OSINT_SHOP_LOC.findall(body)
        if kids and ('<sitemapindex' in body or all(k.endswith('.xml')
                                                    or 'from=' in k for k in kids)):
            for k in kids:
                kind = ('products' if 'product' in k else
                        'collections' if 'collect' in k or 'categor' in k
                        else 'other')
                out['sitemaps'][kind].append(k)
        else:
            kind = ('products' if 'product' in m else
                    'collections' if 'collect' in m or 'categor' in m
                    else 'other')
            out['sitemaps'][kind].append(m)
    for s in out['sitemaps']['products'][:pages]:
        try:
            body = httpx.get(s, headers=OSINT_SHOP_UA, follow_redirects=True,
                             timeout=timeout).text
        except httpx.HTTPError:
            continue
        out['products'] += len(OSINT_SHOP_LOC.findall(body))
        out['titles'] += OSINT_SHOP_TITLE.findall(body)
        out['handles'] += [k.rsplit('/', 1)[-1]
                           for k in OSINT_SHOP_LOC.findall(body)]
    for s in out['sitemaps']['collections'][:pages]:
        try:
            body = httpx.get(s, headers=OSINT_SHOP_UA, follow_redirects=True,
                             timeout=timeout).text
        except httpx.HTTPError:
            continue
        out['collections'] += [k.rsplit('/', 1)[-1]
                               for k in OSINT_SHOP_LOC.findall(body)]
    out['titles'] = list(dict.fromkeys(t.strip() for t in out['titles']))[:80]
    out['handles'] = list(dict.fromkeys(out['handles']))[:40]
    out['collections'] = list(dict.fromkeys(
        c for c in out['collections'] if c and c != 'collections'))[:60]
    out['ok'] = bool(out['titles'] or out['handles'] or out['collections']
                     or out['platform'])
    return out


def osint_shop_models(titles: list) -> dict:
    """Номера-«артикулы» из названий товаров: токен → сколько раз встречен.

    Номера вида `156`, `14960-051`, `4179` — модельная нумерация бренда;
    повторяясь через витрины, она связывает их крепче доменов.

    Args:
        titles: список названий (обычно `osint_shop_map()['titles']`).

    Returns:
        {'tokens': {номер: сколько раз}, 'count': int} — по убыванию встреч.
    """
    tokens: dict[str, int] = {}
    for t in titles:
        for m in OSINT_SHOP_MODEL.findall(t):
            tokens[m] = tokens.get(m, 0) + 1
    top = dict(sorted(tokens.items(), key=lambda x: -x[1]))
    return {'tokens': top, 'count': len(top)}


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('url')
    _a = _P.parse_args()
    _res = osint_shop_map(_a.url)
    print('платформа:', _res['platform'] or '?', '| товаров в sitemap:',
          _res['products'], '| жетонов моделей:',
          osint_shop_models(_res['titles'])['count'], '|', _res['error'])
    print('коллекции:', ', '.join(_res['collections']) or '—')
    for _t in _res['titles'][:20]:
        print(' •', _t)
    raise SystemExit(0 if _res['ok'] else 1)

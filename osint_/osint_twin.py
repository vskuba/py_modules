"""Байт-в-байт близнецы: какие сайты — одна рука, даже под разными вывесками.

`osint_flavor` отвечает «что это за хост», `web_fingerprint` — «на чём движок».
Здесь третий вопрос — «чей»: сеть прячется за десятком доменов, но robots.txt,
favicon, страница 404 и подпись в подвале генерирует один шаблон, и они
совпадают побайтно. Осмотр N сайтов руками — каждый раз curl + md5 по каждому
слою, таблица на пол-экрана и риск сравнить не то; здесь — один проход:
пять слоёв на хост и кластеризация хостов в группы байт-близнецов (union-find
через любой общий слой).

## Что здесь есть

* `osint_twin_probe` — один сайт: A-запись и /24 (через DoH — локальный DNS
  врёт и молчит), sha robots.txt, sha favicon, sha страницы 404, подписи из
  разметки («Створено», «Created by», «© …»); с `deep=True` заглядывает и в
  скрипты того же хоста — SPA-шаблон держит подвал именно там;
* `osint_twin_group` — пачка: пробы параллельно, карта «слой → хеш → хосты»
  и clusters — связные группы близнецов с указанием, каким слоем склеены.

⚠ Штатный favicon или robots движка (дефолт OpenCart, Tilda) склеит в кластер
несвязанные сайты — смотри населённость слоя в `shares`: тридцать хостов на
одной фавиконке — не родство, а общий движок.
⚠ CDN (Cloudflare) закрывает и IP, и свою 404-страницу — эти слои молчат или
врут; robots и подвал обычно остаются родные.
"""
import asyncio
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import httpx

from net_.net_who import net_who_dns

OSINT_TWIN_UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120'}
OSINT_TWIN_404 = '/osint-twin-404-7c3f'  # заведомо мертвый путь: ответ = печать 404-страницы
OSINT_TWIN_SIGN = re.compile(
    r'(?:створено|створен[оіу]?|створен[оіу]?\s+сайти?|created\s+(?:by|with)|made\s+(?:by|with)'
    r'|powered\s+by|розробка|разработка|design(?:ed|s)?\s+by|©)'
    r'[^<]{0,50}', re.I)


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12] if data else ''


def _get(client: httpx.Client, url: str) -> tuple:
    """GET с падением http://; вернёт (status, bytes) или (None, b'')."""
    for candidate in (url, url.replace('https://', 'http://', 1)):
        try:
            r = client.get(candidate, follow_redirects=True)
            return r.status_code, r.content
        except httpx.HTTPError:
            continue
    return None, b''


OSINT_TWIN_SCRIPT = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)


def _scripts(html: bytes, base: str, limit: int = 6) -> list:
    """Скрипты того же хоста из разметки: внешние CDN — не подпись автора."""
    found, seen = [], set()
    for src in OSINT_TWIN_SCRIPT.findall(html.decode('utf-8', 'replace')):
        url = urljoin(base + '/', src)
        if urlparse(url).netloc == urlparse(base).netloc and url not in seen:
            seen.add(url)
            found.append(url)
    return found[:limit]


def osint_twin_probe(target: str, *, timeout: float = 12.0, deep: bool = False) -> dict:
    """Один сайт: сетевой слой и четыре побайтных отпечатка.

    Args:
        target: домен или URL (схема не важна, берётся хост).
        timeout: секунд на слой.
        deep: залезть и в скрипты того же хоста — у SPA подвал живёт там.

    Returns:
        {'ok', 'target', 'ip', 'net': 'a.b.c.0/24', 'robots', 'favicon',
         'notfound', 'footer': sha по множеству подписей, 'marks': [подписи],
         'error'} — хеш пустой, где слой не ответил.
    """
    host = re.sub(r'^https?://', '', target).split('/')[0]
    out = {'ok': False, 'target': host, 'ip': '', 'net': '', 'robots': '',
           'favicon': '', 'notfound': '', 'footer': '', 'marks': [], 'error': ''}
    try:
        dns = asyncio.run(net_who_dns(host, types=('A',)))
        ips = dns['records'].get('A', [])
        out['ip'] = ips[0] if ips else ''
        if out['ip']:
            out['net'] = '.'.join(out['ip'].split('.')[:3]) + '.0/24'
    except Exception as exc:  # DoH недоступен — сетевой слой молчит, дальше идём
        out['error'] = f'dns: {exc}' if not out['error'] else out['error']
    base = f'https://{host}'
    with httpx.Client(headers=OSINT_TWIN_UA, timeout=timeout,
                      follow_redirects=True) as client:
        code, body = _get(client, base + '/robots.txt')
        out['robots'] = _sha(body) if code == 200 and body.strip() else ''
        code, body = _get(client, base + '/favicon.ico')
        out['favicon'] = _sha(body) if code == 200 and body else ''
        code, body = _get(client, base + OSINT_TWIN_404)
        out['notfound'] = _sha(body) if body else ''
        code, home = _get(client, base + '/')
        text = home.decode('utf-8', 'replace')
        if deep:
            for url in _scripts(home, base):
                code, chunk = _get(client, url)
                text += '\n' + chunk.decode('utf-8', 'replace')
        text = re.sub(r'<[^>]+>', ' ', text)
        marks = sorted({re.sub(r'\s+', ' ', m).strip().lower()
                        for m in OSINT_TWIN_SIGN.findall(text) if len(m.strip()) > 2})
        out['marks'] = marks[:8]
        out['footer'] = _sha('|'.join(marks).encode('utf-8')) if marks else ''
    out['ok'] = any(out[k] for k in ('ip', 'robots', 'favicon', 'notfound', 'footer'))
    return out


def osint_twin_group(targets, *, workers: int = 8, timeout: float = 12.0,
                     deep: bool = False) -> dict:
    """Пачка проб и кластеры байт-близнецов.

    Args:
        targets: список доменов/URL (или один строкой).
        workers: параллельных проб (хостеры не любят гонку).
        timeout: секунд на слой внутри пробы.
        deep: пробы с заходом в скрипты (медленнее, но подвал SPA не молчит).

    Returns:
        {'ok', 'probes': [dict из `osint_twin_probe`],
         'shares': {слой: {хеш: [цели]}},          # только хеши с 2+ целями
         'clusters': [{'members': […], 'via': {слой: хеш}}]}  # union-find, size ≥ 2
    """
    if isinstance(targets, str):
        targets = [targets]
    with ThreadPoolExecutor(workers) as pool:
        probes = list(pool.map(lambda t: osint_twin_probe(t, timeout=timeout,
                                                         deep=deep), targets))
    layers = ('net', 'robots', 'favicon', 'notfound', 'footer')
    shares = {}
    for layer in layers:
        by = {}
        for p in probes:
            h = p[layer]
            if h:
                by.setdefault(h, []).append(p['target'])
        shares[layer] = {h: v for h, v in by.items() if len(v) > 1}
    parent = {p['target']: p['target'] for p in probes if p['ok']}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for hashed in shares.values():
        for names in hashed.values():
            for n in names[1:]:
                a, b = find(names[0]), find(n)
                if a != b:
                    parent[b] = a
    groups = {}
    for t in parent:
        groups.setdefault(find(t), []).append(t)
    clusters = []
    for members in groups.values():
        if len(members) > 1:
            layer_of = {}
            for layer, hashed in shares.items():
                for h, names in hashed.items():
                    if len(set(names) & set(members)) > 1:
                        layer_of[layer] = h
            clusters.append({'members': sorted(members), 'via': layer_of})
    return {'ok': True, 'probes': probes, 'shares': shares,
            'clusters': sorted(clusters, key=lambda c: -len(c['members']))}


if __name__ == '__main__':
    import argparse
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('targets', nargs='*', help='домены или файл со списком')
    _P.add_argument('--workers', type=int, default=8)
    _P.add_argument('--timeout', type=float, default=12.0)
    _P.add_argument('--deep', action='store_true', help='подвал искать и в скриптах хоста')
    _a = _P.parse_args()
    src = []
    for n in _a.targets:
        try:
            src += [x.strip() for x in open(n) if x.strip() and not x.startswith('#')]
        except OSError:
            src.append(n)
    got = osint_twin_group(src, workers=_a.workers, timeout=_a.timeout, deep=_a.deep)
    print('target\tip\tnet\trobots\tfavicon\tnotfound\tfooter\tmarks')
    for p in got['probes']:
        print(f"{p['target']}\t{p['ip']}\t{p['net']}\t{p['robots']}\t{p['favicon']}"
              f"\t{p['notfound']}\t{p['footer']}\t{'; '.join(p['marks'][:3])}")
    for c in got['clusters']:
        print(f"\nкластер ×{len(c['members'])}: {', '.join(c['members'])}")
        for layer, h in c['via'].items():
            print(f"  {layer} = {h}")

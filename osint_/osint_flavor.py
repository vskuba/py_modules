"""Ценз имён по вкусу ответа: кто живой, кто пустой слот, кто чей.

Сотни имён из CT-переписи (`osint_ct`) — это список подозреваемых, а не
улика: большинство — пустые слоты генератора, снявшие сертификат и уснувшие.
Осмотр каждого браузером дорог, curl без догадок о путях — дёшев: корень
рассказывает о себе сам, не сходя с места. Поздравилка «сайт создан» —
брошенный лендинг-конвейер; «домен не настроен» — чужой бокс-заглушка;
форма с utm-куками — самописная landing-ферма; каталог с оплатой — витрина.
Руками это каждый раз один цикл с маркерами; здесь — пачка, параллель и
таблица классов.

## Что здесь есть

* `osint_flavor_probe` — один хост: DNS, GET корня без догадок, класс по
  маркерам + какие Set-Cookie выставил (с зондом `?utm_source=probe`);
* `osint_flavor_sweep` — пачка имён параллельно, TSV--ready строки.

⚠ Класс по вкусу — поверхностная догадка: лендинг-ферма может отдать 404,
а витрина — заглушку. Класс — повод копать дальше, не вывод.
⚠ Отсутствующий ответ ≠ мёртвое имя: TLS без SNI и порт, закрытый вчера,
выглядят как down.
"""
from concurrent.futures import ThreadPoolExecutor

import httpx

GHOST = ('не настроен', 'not configured', 'домен не привязан', 'parking',
         'domain is parked', 'not found by server')
CONGRAT = ('поздравляем', 'сайт создан', 'лендинг', 'ваш сайт уже')
SHOP = ('okaycms', 'oct_', 'opencart', 'yml_catalog', 'корзина')
LAND = ('<form', 'utm_source', 'política de privacidad', 'privacidad')

_FLAVOR_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 probe'


def osint_flavor_probe(name: str, *, utm: bool = True, timeout: float = 10.0) -> dict:
    """Один хост: чем он отвечает с корня и какие куки раздает.

    Args:
        name: имя хоста (`auebox.shop`, `cooker.midorile.shop`).
        utm: True — добавить `?utm_source=probe&utm_medium=probe` и
            записать, какие utm-куки хост выставил (печать конвейера).
        timeout: секунд на GET (DNS считается тем же клиентом).

    Returns:
        {'ok': bool, 'name': …, 'ip': '…'|'', 'code': int|0, 'size': int,
         'class': 'nx|dns|down|ghost|congrat|land|shop|page', 'cookies': […],
         'error': словами (пусто при удаче)}
    """
    url = f'http://{name}/' if ':' not in name else name
    try:
        r = httpx.get(url, params=({'utm_source': 'probe', 'utm_medium': 'probe'}
                                   if utm else None), timeout=timeout,
                      follow_redirects=True, headers={'User-Agent': _FLAVOR_UA})
    except httpx.HTTPError as e:
        why = str(e).lower()
        cls = 'nx' if 'nodename' in why or 'nameresolution' in why else 'dns' \
            if 'connect' in why and 'refused' in why else 'down'
        return {'ok': False, 'name': name, 'ip': '', 'code': 0, 'size': 0,
                'class': cls, 'cookies': [], 'error': f'{type(e).__name__}'}
    body = r.text.lower()
    cookies = [c.name for c in r.cookies]
    cls = 'page'
    if any(m in body for m in GHOST):
        cls = 'ghost'
    elif any(m in body for m in CONGRAT):
        cls = 'congrat'
    elif any(m in body for m in SHOP) and r.status_code == 200:
        cls = 'shop'
    elif any(m in body for m in LAND) and r.status_code == 200:
        cls = 'land'
    elif r.status_code == 404:
        cls = 'ghost' if not body.strip() else cls
    return {'ok': True, 'name': name, 'ip': str(r.url), 'code': r.status_code,
            'size': len(body), 'class': cls, 'cookies': cookies, 'error': ''}


def osint_flavor_sweep(names, *, workers: int = 12, utm: bool = True,
                       timeout: float = 10.0) -> list:
    """Пачка имён параллельно; строки в порядке names, TSV-ready.

    Args:
        names: список имён (CT-перепись, список из логов).
        workers: параллельных проверок (не разгонять — у хостеров rate-limit).
        utm: зонд utm-куки на каждом живом хосте.
        timeout: секунд на хост.

    Returns:
        список dict из `osint_flavor_probe` в порядке names.
    """
    if isinstance(names, str):
        names = [names]
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda n: osint_flavor_probe(n, utm=utm,
                                                         timeout=timeout), names))


if __name__ == '__main__':
    import argparse
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('names', nargs='*', help='имена или файл со списком')
    _P.add_argument('--workers', type=int, default=12)
    _P.add_argument('--no-utm', action='store_true', help='без utm-зонда')
    _a = _P.parse_args()
    src = []
    for n in _a.names:
        try:
            src += [x.strip() for x in open(n) if x.strip() and not x.startswith('#')]
        except OSError:
            src.append(n)
    for row in osint_flavor_sweep(src, workers=_a.workers, utm=not _a.no_utm):
        print(f"{row['name']}\t{row['code']}\t{row['size']}\t{row['class']}"
              f"\t{' '.join(row['cookies'])}\t{row['error']}")

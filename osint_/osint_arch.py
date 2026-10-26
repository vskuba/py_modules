"""Архивные снимки: что сайт показывал раньше, когда ныне мёртв.

Wayback — единственный свидетель, который не спорит и не выключается: у
домена, чей origin сдох за CDN (SSL 525 на всех путях), у витрины, снятой
на конвейер, у лендинга, прожившего неделю, — у всех них есть копия в
архиве, с датой. Руками это каждый раз одна возня: CDX лежит «Temporarily
Offline» на часы и сутки, JSON приходится отличать от HTML-заглушки, а
сырую копию надо звать с `id_`, иначе архив подмешает свою обвязку. Здесь —
перепись снимков с retry и загрузка ровно той версии, что названа.

## Что здесь есть

* `osint_arch_snapshots` — перепись снимков URL: первый, последний, год за
  годом (или все), только 200-е по умолчанию;
* `osint_arch_fetch` — сырая копия версии (без архивной обвязки).

⚠ Архив ловит то, что проиндексировал краулер: тёмные страницы лендинга
там может не быть вовсе — это не «страницы не существовало».
⚠ Первый снимок — потолок возраста витрины, не дата её запуска: между
появлением и первым визитом краулера — недели.
"""
import time

import httpx

CDX_URL = 'https://web.archive.org/cdx/search/cdx'
ARCH_BASE = 'https://web.archive.org/web/'


def osint_arch_snapshots(url: str, *, from_: str = '', to: str = '',
                         everything: bool = False, attempts: int = 5,
                         retries_wait: float = 10.0,
                         timeout: float = 30.0) -> dict:
    """Перепись снимков URL; «Temporarily Offline» пересидивается backoff'ом.

    Args:
        url: адрес (без схемы тоже понимается).
        from_, to: границы, `ГГГГ` или `ГГГГММДД`.
        everything: True — все версии; False — по снимку на год (collapse).
        attempts: попыток (CDX бывает offline — это пауза, не ошибка).
        retries_wait: фундамент backoff между попытками, секунд.
        timeout: секунд на запрос.

    Returns:
        {'ok': bool, 'url': …, 'count': int,
         'first': {'ts','status','url'}|None, 'last': …|None,
         'rows': […], 'error': словами (пусто при удаче)}
    """
    rows, err = [], ''
    for n in range(attempts):
        if n:
            time.sleep(retries_wait * n)
        try:
            r = httpx.get(CDX_URL, params={
                'url': url, 'output': 'json', 'filter': 'statuscode:200',
                'fl': 'timestamp,original,statuscode',
                **({} if everything else {'collapse': 'timestamp:6'}),
                **({'from': from_} if from_ else {}),
                **({'to': to} if to else {}),
                **({} if everything else {'limit': '-1000'})},
                timeout=timeout, follow_redirects=True)
            js = r.json() if 'json' in r.headers.get('content-type', '') else None
        except (httpx.HTTPError, ValueError):
            js = None
        if isinstance(js, list):
            # первой строкой CDX всегда отдаёт заголовок «timestamp original…»
            rows = [x for x in js if isinstance(x, list)
                    and not (x and x[0] == 'timestamp')]
            err = ''
            break
        err = 'CDX: offline или мусор'
    slim = [{'ts': x[0], 'url': x[1], 'status': x[2]} for x in rows]
    return {'ok': bool(slim), 'url': url, 'count': len(slim),
            'first': slim[0] if slim else None,
            'last': slim[-1] if slim else None, 'rows': slim, 'error': err}


def osint_arch_fetch(url: str, ts: str = '', *, timeout: float = 45.0) -> dict:
    """Сырая копия версии (`ts` — из `osint_arch_snapshots`); без обвязки архива.

    Args:
        url: адрес.
        ts: снимок, `ГГГГММДДГГММСС`; пусто — ближайший к нашему времени.
        timeout: секунд на загрузку.

    Returns:
        {'ok': bool, 'url': …, 'ts': …, 'body': '…', 'error': словами}
    """
    full = f'{ARCH_BASE}{ts or "2026"}id_/{url}'
    try:
        r = httpx.get(full, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        return {'ok': False, 'url': url, 'ts': ts, 'body': '',
                'error': f'{type(e).__name__}'}
    return {'ok': r.status_code == 200, 'url': url, 'ts': ts,
            'body': r.text, 'error': '' if r.status_code == 200 else f'HTTP {r.status_code}'}


if __name__ == '__main__':
    import argparse
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('url')
    _P.add_argument('--from', dest='from_', default='')
    _P.add_argument('--to', default='')
    _P.add_argument('--all', dest='everything', action='store_true')
    _P.add_argument('--get', default='', help='снимок для сырой копии')
    _P.add_argument('--out', default='', help='файл для копии')
    _a = _P.parse_args()
    if _a.get:
        res = osint_arch_fetch(_a.url, _a.get)
        if res['ok']:
            if _a.out:
                with open(_a.out, 'w', encoding='utf-8') as fh:
                    fh.write(res['body'])
            else:
                import sys
                sys.stdout.write(res['body'])
        else:
            print('не вышло:', res['error'])
    else:
        res = osint_arch_snapshots(_a.url, from_=_a.from_, to=_a.to,
                                   everything=_a.everything)
        print('снимков:', res['count'], res['error'])
        for x in res['rows']:
            print(x['ts'], x['status'], x['url'])

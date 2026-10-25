"""Цепочки редиректов пачкой: canonical-шов сборки виден матрицей.

Вопрос «куда на самом деле ведёт домен» руками решается `curl -IL` на
каждый URL — и каждая пачка доменов собирается заново, а вывод склеивается
из http_code и url_effective. Здесь то же самое — обход без
`follow_redirects`, hop за hop, пока не упрёмся в тело или потолок; пачка —
параллельно. Матрица из финальных адресов и отвечает на то, что в разведке
ищут дольше всего: нормализует ли сборка apex/www (и в какую сторону),
ходит ли кто между доменами, жив ли http без HSTS.

## Что здесь есть

* `http_hops_trace` — цепочка одного URL (по голой схеме, без догадок);
* `http_hops_matrix` — пачка URL, плюс сводка: куда нормализует, где кросс.

⚠ HEAD многие отдают 405 там, где GET живой: при пустой цепочке смотри
`method='GET'`. ⚠ 200 без редиректа на http — это обычно не «работает и
так», а отсутствие HSTS; вывод о canonical делай по паре http+https.
"""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import httpx

UA = {'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) hops-probe/1.0'}


def http_hops_trace(url: str, *, method: str = 'GET', max_hops: int = 6,
                    timeout: float = 12.0) -> dict:
    """Цепочка редиректов одного URL, остановлена честно.

    Args:
        url: стартовый адрес.
        method: GET надёжнее HEAD (405 на живых страницах).
        max_hops: потолок переходов — цикл не зациклит.
        timeout: секунд на переход.

    Returns:
        {'ok': bool, 'start': url, 'final': url, 'code': int,
         'hops': [{'url','code','location'}], 'cross': регистр домена
         старта отличается от финала ('' если нет), 'error': словами}
    """
    hops, cur, code, err = [], url, 0, ''
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout,
                          headers=UA) as c:
            for _ in range(max_hops + 1):
                r = c.request(method, cur)
                code = r.status_code
                loc = r.headers.get('location', '')
                hops.append({'url': str(cur), 'code': code, 'location': loc})
                if code not in (301, 302, 303, 307, 308) or not loc:
                    break
                cur = str(httpx.URL(str(cur)).join(loc))
            else:
                err = f'более {max_hops} переходов'
    except httpx.HTTPError as e:
        err = f'{type(e).__name__}'
    final = hops[-1]['url'] if hops else url
    cross = _hops_cross(url, final)
    return {'ok': not err and hops, 'start': url, 'final': str(final),
            'code': code, 'hops': hops, 'cross': cross, 'error': err}


def http_hops_matrix(urls, *, method: str = 'GET', workers: int = 12,
                     timeout: float = 12.0) -> dict:
    """Пачка URL — матрица редиректов и сводка по canonical-поведению.

    Args:
        urls: список адресов.
        method: GET/HEAD.
        workers: параллельных обходов.

    Returns:
        {'ok': bool, 'rows': [то же, что `http_hops_trace`, по порядку],
         'summary': {'to_apex': n, 'to_www': n, 'same_form': n,
                     'cross_domain': n, 'dead': n}}
    """
    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(lambda u: http_hops_trace(u, method=method,
                                                      timeout=timeout), urls))
    s = {'to_apex': 0, 'to_www': 0, 'same_form': 0, 'cross_domain': 0,
         'dead': 0}
    for r in rows:
        if not r['ok'] or r['code'] >= 400:
            s['dead'] += 1
            continue
        if r['cross'] == 'domain':
            s['cross_domain'] += 1
        s[_hops_form(r['start'], r['final'])] += 1
    return {'ok': any(r['ok'] for r in rows), 'rows': rows, 'summary': s}


def _hops_form(start: str, final: str) -> str:
    """Как сменилась форма имени: на apex, на www или та же."""
    h0 = urlparse(start).hostname or ''
    h1 = urlparse(final).hostname or ''
    if h1.lower().startswith('www.') and not h0.lower().startswith('www.'):
        return 'to_www'
    if h0.lower().startswith('www.') and not h1.lower().startswith('www.'):
        return 'to_apex'
    return 'same_form'


def _hops_cross(start: str, final: str) -> str:
    """Кросс-домен или кросс-схема; '' если форма та же."""
    h0 = (urlparse(start).hostname or '').lower().removeprefix('www.')
    h1 = (urlparse(final).hostname or '').lower().removeprefix('www.')
    s0 = (urlparse(start).scheme, urlparse(final).scheme)
    if h0 != h1:
        return 'domain'
    if s0[0] != s0[1]:
        return 'scheme'
    return ''


if __name__ == '__main__':
    import argparse

    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('urls', nargs='+')
    _P.add_argument('--method', default='GET')
    _C = _P.parse_args()
    _res = http_hops_matrix(_C.urls, method=_C.method)
    for _r in _res['rows']:
        _via = ' -> '.join(f"{h['url']} [{h['code']}]" for h in _r['hops'])
        print(_via + (f"  ERR={_r['error']}" if _r['error'] else ''))
    print('summary:', _res['summary'])
    raise SystemExit(0 if _res.get('ok') else 1)

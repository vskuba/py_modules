"""CT-перепись: какие имена когда-либо были выпущены на корни доменов.

CT-лог не врал про живучесть, но и про молчание тоже: имя, с которого
снимался сертификат, либо живое имя, либо пустой слот генератора — кладбища
нет. Перепись маской `%.<root>` по списку корней — индекс всех когда-либо
выпущенных имён, дешевле и полнее любого собственного списка. Руками это
каждый раз один и тот же цикл: crt.sh отвечает то HTML-ошибкой, то пустым
JSON, а при наглости банит корень на сутки — здесь пауза между корнями,
backoff на ошибках и дедуп имён (www, мульти-SAN, wildcard-мусор).

## Что здесь есть

* `osint_ct_names` — пачка корней → уникальные имена каждого корня и все
  вместе; бан Root'а виден как ошибка, остальные не трогаются.

⚠ Имя из CT — не «живой сайт», а факт «когда-то просили сертификат»:
классифицировать живой/пустой слот нужно_http-пробой (`osint_flavor`).
⚠ crt.sh считает уникальность по сертификатам: одно имя в двадцати
сертификатах — двадцать строк; отсюда дедуп на выходе.
"""
import time

import httpx

CT_URL = 'https://crt.sh/'


def osint_ct_names(roots, *, workers_hint: float = 1.5, attempts: int = 4,
                   timeout: float = 30.0, retries_wait: float = 6.0,
                   keep_www: bool = False) -> dict:
    """Перепись CT-имён пачкой корней; бан одного корня не ломает пачку.

    Args:
        roots: строка или список корней (`auebox.shop`, `trend-house.shop`).
        workers_hint: пауза между корнями, секунд (crt.sh не любит шустрых).
        attempts: попыток на корень; между ними exponential backoff.
        timeout: секунд на один запрос crt.sh.
        retries_wait: фундамент backoff между попытками, секунд.
        keep_www: False — `www.х` схлопнут в `х` (тот же сайт, другое имя).

    Returns:
        {'ok': bool, 'roots': {root: {'names': […], 'error': ''|'…'}},
         'names': [уникальные имена по всем корням, sorted]}
    """
    if isinstance(roots, str):
        roots = [roots]
    out, allnames = {}, set()
    for i, root in enumerate([r.strip().lower().lstrip('%.') for r in roots if r.strip()]):
        names, err = [], ''
        for n in range(attempts):
            if n:
                time.sleep(retries_wait * (2 ** (n - 1)))
            try:
                r = httpx.get(CT_URL, params={'q': f'%.{root}', 'output': 'json'},
                              timeout=timeout, follow_redirects=True)
                rows = r.json() if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                rows = []
            if isinstance(rows, list) and rows:
                for row in rows:
                    for nm in str(row.get('name_value', '')).split('\n'):
                        nm = nm.strip().lower().lstrip('*.')
                        if nm and nm.endswith(f'.{root}') and '*' not in nm:
                            names.append(nm)
                break
            err = 'crt.sh: пусто, HTML-ошибка или бан корня'
        if not keep_www:
            names = sorted({n[4:] if n.startswith('www.') else n for n in names})
        else:
            names = sorted(set(names))
        out[root] = {'names': names, 'error': err if not names else ''}
        allnames.update(names)
        if i < len(roots) - 1:
            time.sleep(workers_hint)
    return {'ok': any(v['names'] for v in out.values()), 'roots': out,
            'names': sorted(allnames)}


if __name__ == '__main__':
    import argparse
    import sys
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('roots', nargs='*', help='корни или файл со списком (по строке)')
    _P.add_argument('--www', action='store_true', help='не схлопывать www.')
    _a = _P.parse_args()
    src = []
    for r in _a.roots:
        try:
            src += [x.strip() for x in open(r) if x.strip() and not x.startswith('#')]
        except OSError:
            src.append(r)
    res = osint_ct_names(src, keep_www=_a.www)
    for root, v in res['roots'].items():
        for nm in v['names']:
            print(f'{root}\t{nm}')
        if v['error']:
            print(f'{root}\tОШИБКА: {v["error"]}', file=sys.stderr)
    print(f'всего уникальных: {len(res["names"])}', file=sys.stderr)

"""PTR-перепись блоков адресов: реверс-зона хостера как карта машин.

`net_who_dns` смотрит на домен; здесь — наоборот, на адрес: кто на нём
подписан. Реверс-зона хостера (default-host.net, contaboserver.net и т.п.)
отдаёт подноменклатуру его машин дешевле CT-лога: переписать квартал
`2a06:6440:0:2d00::/120` или `185.104.45.0/24` — и видно, какие имена живут
рядом с витринами, сколько адресов держит каждый фронт, где заглушки
хостинга. Руками это каждый раз одинаковые тридцать строк с
ThreadPoolExecutor и dig; здесь — одним вызовом через DoH (dns.google, как
у `net_who`, без dig и сидов).

## Что здесь есть

* `net_ptr_get` — PTR одного адреса (v4/v6);
* `net_ptr_sweep` — пачкой адреса/CIDR, параллельно, с группировкой имён
  в пулы: имя → все адреса, у которых этот PTR.

⚠ Соседство адресов в квартале ничего не доказывает: business-N и чужие
клиенты лежат вперемешку с сетевыми машинами просто потому, что пул общий.
Перепись — поверхность перечисления, а не вывод о владельце.
⚠ Пустой PTR у свежего allocation — не «мёртвый адрес», а ещё не подписанный.
"""
import ipaddress
from concurrent.futures import ThreadPoolExecutor

import httpx

DOH = 'https://dns.google/resolve'


def net_ptr_get(ip: str, *, timeout: float = 8.0) -> str:
    """Имя по реверсу одного адреса; '' если реверса нет.

    Args:
        ip: IPv4 или IPv6.
        timeout: секунд на запрос.
    """
    return _net_ptr_answer(_net_ptr_arpa(ip), timeout)


def net_ptr_sweep(targets, *, workers: int = 24, timeout: float = 8.0,
                  cap: int = 4096) -> dict:
    """Перепись реверсов пачкой адресов/CIDR; имена сгруппированы в пулы.

    Args:
        targets: строка или список; элемент — адрес или CIDR
            (`185.104.45.0/24`, `2a06:6440:0:2d00::/120`).
        workers: параллельных запросов.
        timeout: секунд на ответ DoH.
        cap: потолок адресов с одного CIDR — защита от пальца в /8.

    Returns:
        {'ok': bool, 'probed': int, 'named': int,
         'by_name': {имя PTR: [адреса…]},   # '' = без реверса
         'map': {адрес: имя|''}}           # порядок = порядок targets
    """
    ips = _net_ptr_expand(targets, cap)
    with ThreadPoolExecutor(workers) as pool:
        names = list(pool.map(lambda ip: _net_ptr_answer(_net_ptr_arpa(ip),
                                                        timeout), ips))
    mapping = dict(zip(ips, names))
    by_name: dict = {}
    for ip, name in mapping.items():
        by_name.setdefault(name, []).append(ip)
    return {'ok': any(mapping.values()), 'probed': len(ips),
            'named': sum(1 for n in mapping.values() if n),
            'by_name': dict(sorted(by_name.items(), key=lambda kv: -len(kv[1]))),
            'map': mapping}


def _net_ptr_expand(targets, cap: int) -> list:
    """Адреса и CIDR — в один плоский список без повторов."""
    if isinstance(targets, str):
        targets = [targets]
    out, seen = [], set()
    for t in targets:
        t = t.strip()
        if not t:
            continue
        if '/' in t:
            net = ipaddress.ip_network(t, strict=False)
            hosts = [str(h) for h in net.hosts()] or [str(net.network_address)]
            for ip in hosts[:cap]:
                if ip not in seen:
                    seen.add(ip)
                    out.append(ip)
        elif t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _net_ptr_arpa(ip: str) -> str:
    """Имя arpa для реверса: v4 — октеты назад, v6 — нибблы назад."""
    addr = ipaddress.ip_address(ip)
    if addr.version == 4:
        return '.'.join(str(addr).split('.')[::-1]) + '.in-addr.arpa'
    hexed = addr.exploded.replace(':', '')
    return '.'.join(hexed[::-1]) + '.ip6.arpa'


def _net_ptr_answer(name: str, timeout: float) -> str:
    """Один PTR через DoH; пустая строка — реверса нет или запрос не удался."""
    try:
        r = httpx.get(DOH, params={'name': name, 'type': 'PTR'}, timeout=timeout)
        answers = r.json().get('Answer') or []
    except (httpx.HTTPError, ValueError):
        return ''
    for a in answers:
        data = (a.get('data') or '').rstrip('.').strip()
        if data:
            return data
    return ''


if __name__ == '__main__':
    import argparse
    import json

    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('cmd', choices=('get', 'sweep'))
    _P.add_argument('targets', nargs='+', help='IP или CIDR')
    _P.add_argument('--workers', type=int, default=24)
    _C = _P.parse_args()
    if _C.cmd == 'get':
        _res = {'ok': True, 'name': net_ptr_get(_C.targets[0])}
        print(json.dumps(_res, ensure_ascii=False))
    else:
        _res = net_ptr_sweep(_C.targets, workers=_C.workers)
        print(f'probed={_res["probed"]} named={_res["named"]}')
        for _n, _ips in _res['by_name'].items():
            if not _n:
                continue
            print(f'{_n:<45} ×{len(_ips):<4} {_ips[0]}'
                  + (f' … (+{len(_ips) - 1})' if len(_ips) > 1 else ''))
    raise SystemExit(0 if _res.get('ok') else 1)

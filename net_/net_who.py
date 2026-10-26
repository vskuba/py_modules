"""Кто хостит и где живёт адрес: DNS-записи и владелец IP, без ключей.

В разведке сайта третий вопрос после «что за движок» — «кто его крутит»: NS-серверы
и AS номера выдают хостинг, хостинг подсказывает, где кабинет и почтовый вход, а до
порта дотягиваешься уже зная куда. `net_port` проверял порт; здесь — остальное:
записи домена (DoH dns.google, без ключей и сидов) и AS-владелец IP (rdap.org).

## Что здесь есть

* `net_who_dns` — A/AAAA/NS/MX/TXT домена через DoH;
* `net_who_aa` — что отдают сами авторитетные NS, рядом с рекурсивным ответом;
* `net_who_ip` — ASN, владелец и страна по IP (rdap bootstrap → реестр);
* `net_who_reg` — дата регистрации/истечения и регистратор (RDAP, с запасным
  whois-43);
* `net_who_sweep` — пачке доменов одна таблица слоёв: адреса, NS, MX, SPF,
  даты — веер наблюдения за сетью одним прогоном.

⚠ DoH-ответ разбирается как есть: `net_who_dns` отдаёт сырые поля `name/value`
по типу, ничего не додумывая — разбор у вызывающего.
"""
import asyncio
import httpx
import json
import re
import shutil
import socket
import subprocess

DOH = 'https://dns.google/resolve'
RDAP = 'https://rdap.org'


async def net_who_dns(domain: str, types=('A', 'AAAA', 'NS', 'MX', 'TXT')) -> dict:
    """Записи домена через DoH (dns.google), без ключа и без локального резолвера.

    Args:
        domain: имя домена.
        types: какие записи снимать (A/AAAA/NS/MX/TXT/CNAME).

    Returns:
        {'ok': хоть что-то, 'domain': домен,
         'records': {'A': […], 'NS': […], …}} — пусто где не нашлось.
    """
    out = {}
    async with httpx.AsyncClient(timeout=10) as c:
        for t in types:
            try:
                r = await c.get(DOH, params={'name': domain, 'type': t})
                js = r.json()
                out[t] = [a.get('data', '') for a in js.get('Answer', [])]
            except (httpx.HTTPError, ValueError, AttributeError):
                out[t] = []
    return {'ok': any(out.values()), 'domain': domain, 'records': out}


# Рекурсивный резолвер отвечает то, что видят все; NS зоны — то, что в зоне
# лежит на самом деле. Ответы расходятся, когда имя посреди переезда
# делегировано на новый бокс, когда зону отдаёт бренд-защита (CSC и подобные),
# или когда зону «забыли» в старом кабинете. Без вопроса к самим NS этого не
# увидеть: путь
# через корневую иерархию один. Здесь системный dig: руками DNS-пакет писать —
# ловить чужие баги, dig написан именно для этого.
AA_DIG = ('dig', '+noall', '+comments', '+answer', '+time=4', '+tries=1')


async def net_who_aa(domain: str, servers: list = (),
                     types=('A', 'AAAA')) -> dict:
    """Что отдают авторитетные NS зоны напрямую — и расходится ли это с общим DNS.

    Аргументы:
        domain: имя домена.
        servers: NS-хосты для вопроса; пусто — те, что отдаёт `net_who_dns`.
        types: какие записи снимать с каждого NS (A/AAAA/CNAME).

    Возвращает:
        {'ok': хоть кто-то ответил, 'domain': …, 'ns': […],
         'recursive': {'A': […], …} — что видят все,
         'servers': {'ns.name': {'aa': bool, 'A': […], 'soa': 'сериал'| '',
                                 'error': словами}},
         'diff': разошёлся ли A авторитетный с рекурсивным,
         'error': причина (пусто при удаче)}

    ⚠⚠ `diff` — поверхность, а не вывод: NS вправе отвечать по-разному
    (split-horizon, геораздача, зеркала в процессе правки зоны). Польза для
    разведки — сам факт, кому и куда указывает зона без посредников.
    """
    if not shutil.which('dig'):
        return {'ok': False, 'domain': domain, 'ns': [], 'recursive': {},
                'servers': {}, 'diff': False, 'error': 'в PATH нет dig'}
    rec = await net_who_dns(domain, ('NS',) + tuple(types))
    ns = list(servers) or rec['records'].get('NS', [])
    servers_out = {}
    for s in ns[:6]:
        row = {'aa': True, 'soa': '', 'error': ''}
        for t in types:
            d = await asyncio.to_thread(_net_who_dig, s, domain, t)
            row[t] = d['values']
            row['aa'] = row['aa'] and d['aa']
            if d['error'] and not row['error']:
                row['error'] = d['error']
        soa = await asyncio.to_thread(_net_who_dig, s, domain, 'SOA')
        row['soa'] = soa['values'][0] if soa['values'] else ''
        servers_out[s] = row
    ra = {t: rec['records'].get(t, []) for t in types}
    diff = any(row.get('A', []) != ra.get('A', [])
               for row in servers_out.values() if 'A' in row)
    return {'ok': bool(servers_out), 'domain': domain, 'ns': ns,
            'recursive': ra, 'servers': servers_out, 'diff': diff,
            'error': '' if servers_out else 'не от кого спрашивать — NS пусты'}


def _net_who_dig(server: str, target: str, rtype: str) -> dict:
    """Один вопрос прямо к NS зоны: авторитетно ли отвечает и что именно."""
    try:
        r = subprocess.run([*AA_DIG, f'@{server}', target, rtype],
                           capture_output=True, text=True, timeout=20)
    except OSError as e:
        return {'aa': False, 'values': [], 'error': f'{type(e).__name__}'}
    except subprocess.TimeoutExpired:
        return {'aa': False, 'values': [], 'error': 'timeout'}
    aa = any(re.search(r'flags:[^;]*\baa\b', ln) for ln in r.stdout.splitlines())
    values = []
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if ln.startswith(';') or parts[2:4] != ['IN', rtype]:
            continue
        # у SOA значение — 7-е поле (после mname и rname идёт серийник),
        # у остальных типов ответ — последнее поле строки.
        values.append(parts[6] if rtype == 'SOA' else parts[-1].rstrip('.'))
    return {'aa': aa, 'values': values, 'error': ''}


async def net_who_ip(ip: str) -> dict:
    """Владелец адреса: ASN, имя организации, страна (rdap bootstrap → реестр).

    Args:
        ip: IPv4/IPv6.

    Returns:
        {'ok': bool, 'ip':…, 'asn':…|'', 'org':…|'', 'country':…|'',
         'error': причина словами (пусто при удаче)}
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as c:
            r = await c.get(f'{RDAP}/ip/{ip}')
            js = r.json()
        org = ''
        for ent in js.get('entities', []):
            for v in ent.get('vcardArray', ['', []])[1]:
                if v[0] == 'fn':
                    org = v[3]
                    break
            if org:
                break
        asn = next((x for x in js.get('handle', '').split('.') if x.isdigit()), '')
        country = (js.get('country') or '').upper()
        return {'ok': True, 'ip': ip, 'asn': asn, 'org': org,
                'country': country, 'error': ''}
    except Exception as e:
        return {'ok': False, 'ip': ip, 'asn': '', 'org': '', 'country': '',
                'error': f'{type(e).__name__}'}


# Кто и когда зарегистрировал домен: RDAP — цивилизованный путь (JSON, без
# парсинга простыней), но не все реестры в нём состоят (.ua среди них), поэтому
# второй путь — живой whois на порту 43, с bootstrap-подсказкой whois.iana.org.
WHOIS_BOOTSTRAP = 'whois.iana.org'
WHOIS_SECOND = ('co', 'com', 'net', 'org', 'edu', 'gov', 'mil', 'in', 'ne',
                'or', 'ac')  # второй уровень у коротких TLD: com.ua — не com
WHOIS_CREATED = re.compile(r'(?i)created\D*?(\d{4}-\d{2}-\d{2})')
WHOIS_EXPIRE = re.compile(r'(?i)expir\w*\D*?(\d{4}-\d{2}-\d{2})')
WHOIS_REF = re.compile(r'(?i)^(?:refer|whois)\s*:\s*(\S+)')
WHOIS_REGISTRAR = re.compile(r'(?i)^registrar\s*:\s*(.+)$', re.M)


async def net_who_reg(domain: str) -> dict:
    """Дата регистрации и регистратор домена: RDAP, а где RDAP молчит — whois-43.

    Args:
        domain: имя домена.

    Returns:
        {'ok': bool, 'domain': …, 'created': 'ГГГГ-ММ-ДД' | '',
         'expires': …| '', 'registrar': …| '', 'source': 'rdap|whois|',
         'error': словами (пусто при удаче)}

    ⚠ Даты — из реестра, а не из жизни сайта: куплен в 2011, наползён в 2026.
    Для вывода «кто вчера встал» дату берут в паре со свежим DNS-первым видением.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as c:
            r = await c.get(f'{RDAP}/domain/{domain}')
        js = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        js = {}
    if isinstance(js, dict) and js.get('events'):
        events = {e.get('eventAction', ''): str(e.get('eventDate', ''))[:10]
                  for e in js['events'] if isinstance(e, dict)}
        registrar = ''
        for ent in js.get('entities', []):
            if 'registrar' in ent.get('roles', []):
                for v in ent.get('vcardArray', ['', []])[1]:
                    if v[0] == 'fn':
                        registrar = v[3]
                        break
            if registrar:
                break
        created = events.get('registration', '')
        if created or registrar:
            return {'ok': True, 'domain': domain, 'created': created,
                    'expires': events.get('expiration', ''),
                    'registrar': registrar, 'source': 'rdap', 'error': ''}
    try:
        row = await asyncio.to_thread(_net_who_whois, domain)
    except OSError as e:
        return {'ok': False, 'domain': domain, 'created': '', 'expires': '',
                'registrar': '', 'source': '', 'error': f'{type(e).__name__}: {e}'}
    return {'ok': bool(row['created'] or row['registrar']), 'domain': domain,
            'source': 'whois', 'error': '' if row['created'] else 'whois не отдал даты',
            **row}


async def net_who_sweep(domains: list,
                        types=('A', 'AAAA', 'NS', 'MX', 'TXT')) -> dict:
    """Пачке доменов — одна таблица слоёв: адреса, NS, MX, SPF, даты регистрации.

    Веер наблюдения за сетью одним прогоном: домены идут параллельно, каждый
    домен спрашивает и DNS, и реестр; чей домен умер — строка с `error`, веер
    не роняет.

    Args:
        domains: список имён.
        types: какие DNS-записи снимать (см. `net_who_dns`).

    Returns:
        {'ok': bool, 'rows': [{'domain', 'a': […], 'ns': […], 'mx': […],
         'spf': 'v=spf1 …'| '', 'created', 'expires', 'registrar', 'error'}]}
    """
    got = await asyncio.gather(*(net_who_dns(d, types) for d in domains))
    regs = await asyncio.gather(*(net_who_reg(d) for d in domains))
    rows = []
    for d, dns, reg in zip(domains, got, regs):
        rec = dns['records']
        rows.append({'domain': d, 'a': rec.get('A', []), 'ns': rec.get('NS', []),
                     'mx': rec.get('MX', []),
                     'spf': next((t for t in rec.get('TXT', [])
                                  if t.startswith('v=spf1')), ''),
                     'created': reg['created'], 'expires': reg['expires'],
                     'registrar': reg['registrar'],
                     'error': '' if dns['ok'] or reg['ok'] else 'пусто по всем слоям'})
    return {'ok': any(not r['error'] for r in rows), 'rows': rows}


def _net_who_whois(domain: str) -> dict:
    """Whois по-старинке: bootstrap у IANA, потом сервер TLD; разбор простыни.

    Синхронное тело (сокеты), наружу идёт через `asyncio.to_thread`.
    """
    labels = domain.split('.')
    tld = '.'.join(labels[-2:]) if len(labels) > 2 and labels[-2] in WHOIS_SECOND \
        else labels[-1]
    server, text = WHOIS_BOOTSTRAP, _net_who_socket(WHOIS_BOOTSTRAP, tld)
    refer = next((m.group(1) for line in text.splitlines()
                  if (m := WHOIS_REF.match(line))), '')
    if refer and refer != WHOIS_BOOTSTRAP:
        text = _net_who_socket(refer, domain)
    created = WHOIS_CREATED.search(text)
    expires = WHOIS_EXPIRE.search(text)
    registrar = WHOIS_REGISTRAR.search(text)
    return {'domain': domain, 'created': created.group(1) if created else '',
            'expires': expires.group(1) if expires else '',
            'registrar': registrar.group(1).strip() if registrar else ''}


def _net_who_socket(server: str, ask: str) -> str:
    """Один вопрос порту 43: отправить строку, прочитать весь ответ."""
    with socket.create_connection((server, 43), timeout=8) as sock:
        sock.sendall((ask + '\r\n').encode('utf-8'))
        chunks = []
        while piece := sock.recv(65536):
            chunks.append(piece)
    return b''.join(chunks).decode('utf-8', 'replace')


if __name__ == '__main__':
    import argparse

    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('cmd', choices=('dns', 'aa', 'ip', 'reg', 'sweep'))
    _P.add_argument('targets', nargs='+', help='домены (или один IP для ip)')
    _C = _P.parse_args()
    if _C.cmd == 'dns':
        _res = asyncio.run(net_who_dns(_C.targets[0]))
    elif _C.cmd == 'aa':
        _res = asyncio.run(net_who_aa(_C.targets[0]))
    elif _C.cmd == 'ip':
        _res = asyncio.run(net_who_ip(_C.targets[0]))
    elif _C.cmd == 'reg':
        _res = asyncio.run(net_who_reg(_C.targets[0]))
    else:
        _res = asyncio.run(net_who_sweep(_C.targets))
    if _C.cmd == 'sweep':
        for _r in _res['rows']:
            print(f"{_r['domain']:<28} created={_r['created'] or '-':<10} "
                  f"ns={','.join(_r['ns']) or '-':<42} mx={_r['mx'][:1] or ['-']} "
                  f"a={','.join(_r['a']) or '-'} err={_r['error']}")
    else:
        print(json.dumps(_res, ensure_ascii=False))
    raise SystemExit(0 if _res.get('ok') else 1)

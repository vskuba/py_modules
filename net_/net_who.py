"""Кто хостит и где живёт адрес: DNS-записи и владелец IP, без ключей.

В разведке сайта третий вопрос после «что за движок» — «кто его крутит»: NS-серверы
и AS номера выдают хостинг, хостинг подсказывает, где кабинет и почтовый вход, а до
порта дотягиваешься уже зная куда. `net_port` проверял порт; здесь — остальное:
записи домена (DoD dns.google, без ключей и сидов) и AS-владелец IP (rdap.org).

## Что здесь есть

* `net_who_dns` — A/AAAA/NS/MX/TXT домена через DoH;
* `net_who_ip` — ASN, владелец и страна по IP (rdap bootstrap → реестр).

⚠ DoH-ответ разбирается как есть: `net_who_dns` отдаёт сыые Namecoin-поля
`name/value` по типу, ничего не додумывая —solution читает caller.
"""
import httpx

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

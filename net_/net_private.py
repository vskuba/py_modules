"""Пришёл ли запрос изнутри: адрес частный, и в цепочке прокси нет публичного.

Внутренняя ручка (вход для планировщика, служебный вебхук) обычно закрыта на прокси:
публичный вход отвечает 403, внутренний порт наружу не проброшен. Но приложение,
целиком доверяющее сети, открывается одной ошибкой в конфиге прокси. Вторая линия —
в самом приложении: ручка пускает, только если запрос пришёл из петли или частной сети
**и** в `X-Forwarded-For` нет ни одного публичного адреса (прокси, пропустивший запрос
снаружи, кладёт туда адрес настоящего клиента).

```python
from net_.net_private import net_private_request

if not net_private_request(request.client.host, request.headers.get('x-forwarded-for')):
    raise HTTPException(403)
```
"""
import ipaddress


def net_private_address(address: str) -> bool:
    """Адрес из петли, частной сети или link-local; имя хоста и мусор — нет.

    Args:
        address: IPv4/IPv6 строкой (IPv4 внутри IPv6 `::ffff:10.0.0.1` разворачивается).

    Returns:
        True — адрес внутренний; False — публичный или не адрес вовсе.
    """
    try:
        ip = ipaddress.ip_address(str(address).strip().split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def net_private_request(client_host: str, forwarded_for: str = None) -> bool:
    """Пришёл ли запрос изнутри: сам клиент внутренний и цепочка прокси без публичных.

    Args:
        client_host: адрес того, кто подключился (`request.client.host`).
        forwarded_for: заголовок `X-Forwarded-For` как есть; пусто — прокси не было.

    Returns:
        True — запрос внутренний; False — снаружи или адрес не разобрать.
    """
    if not net_private_address(client_host):
        return False
    hops = [hop.strip() for hop in str(forwarded_for or '').split(',') if hop.strip()]
    return all(net_private_address(hop) for hop in hops)

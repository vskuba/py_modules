"""
RabbitMQ-инфраструктура проекта: management-API :15672 без менедж-клиента (stdlib).

Два входа в RabbitMQ, оба без пипакетов:
- `rabbitmq_mgmt_get` / `rabbitmq_mgmt_request` — HTTP-менеджмент-API :15672
  (`guest/guest`) для диагностики очередей (`messages_ready`, `consumer_details`)
  и для `vhost:mapping:create`;
- `rabbitmq_queue_publish` — publish в очередь тем же management-API
  (`POST /api/queues/{vhost}/{routing_key}/publish`): сырой AMQP 0-9-1
  сокет-клиент на stdlib не писался — кадровый кодировщик без живого брокера
  не проверить, а management-API отдаёт тот же publish по HTTP.

Пароли из env-файлов (.env.local) в дамп не кладут: модуль читает креды из
аргументов, а не из `.env*`, и наружу пишет только префиксы значений (≤10 символов).
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

# ── константы ──

RABBITMQ_MGMT_PORT = 15672
RABBITMQ_MGMT_AUTH = ('guest', 'guest')  # дефолт-креды: хост и юзер берутся из env проекта
RABBITMQ_MGMT_TIMEOUT = 10
RABBITMQ_MGMT_USER_AGENT = 'python-urllib (dsh tool)'  # «User-Agent вида python-urllib/3.x»

# ── публичный API модуля ──


def rabbitmq_mgmt_request(method: str, host: str = 'rabbitmq', path: str = 'overview',
                          user: str = RABBITMQ_MGMT_AUTH[0], password: str = RABBITMQ_MGMT_AUTH[1],
                          port: int = RABBITMQ_MGMT_PORT, timeout: int = RABBITMQ_MGMT_TIMEOUT,
                          body: dict = None) -> tuple:
    """HTTP-запрос management-API :15672 (basic-auth `guest/guest`, stdlib urllib).

    «Vhost payment_gateway/payment_gateway_test, yml rabbitmq/payment_gateway_vhost.yml,
    `vhost:mapping:create -Hrabbitmq -uguest -pguest`» — креды дефолтными гостями,
    vhost в URL кодуруется (`/` → `%2F`).

    Args:
        method: 'GET' | 'PUT' | 'DELETE' | 'POST'.
        host: хост менеджмент-API (из env/CLAUDE.md, напр. 'rabbitmq').
        path: путь после /api/ (`overview`, `queues`, `queues/%2Fpay/contents`...).
        user, password: креды.
        port: порт management-API; 15672 — дефолт.
        timeout: таймаут коннекта, сек.
        body: тело JSON; None — без тела.

    Returns:
        (parsed, None) — parsed dict|list|str; либо (None, 'текст ошибки с телом').
    """
    url = f'http://{host}:{port}/api/{path.lstrip("/")}'
    auth = base64.b64encode(f'{user}:{password}'.encode('utf-8')).decode('ascii')
    headers = {'Authorization': f'Basic {auth}', 'User-Agent': RABBITMQ_MGMT_USER_AGENT,
               'Accept': 'application/json'}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json;charset=utf-8'
    request = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as err:
        return None, f'{method.upper()} {url} -> HTTP {err.code}: ' \
                     f'{err.read().decode("utf-8", "ignore")[:240]}'
    except urllib.error.URLError as err:
        return None, f'{method.upper()} {url} -> не достать: {err.reason}'
    except OSError as err:
        return None, f'{method.upper()} {url} -> обрыв коннекта: {err}'
    try:
        return (json.loads(payload) if payload.strip() else None), None
    except ValueError:
        return payload, None


def rabbitmq_mgmt_get(host: str = 'rabbitmq', path: str = 'overview',
                      user: str = RABBITMQ_MGMT_AUTH[0], password: str = RABBITMQ_MGMT_AUTH[1],
                      port: int = RABBITMQ_MGMT_PORT, timeout: int = RABBITMQ_MGMT_TIMEOUT) -> tuple:
    """GET management-API :15672 для диагностики очередей (обёртка rabbitmq_mgmt_request).

    Args:
        host: хост менеджмент-API.
        path: путь после /api/ (`overview`, `queues`, `aliveness/test/%2F/pay`, ...).
        user, password: креды.
        port: порт management-API.
        timeout: таймаут коннекта, сек.

    Returns:
        (parsed, None) либо (None, 'текст ошибки').
    """
    return rabbitmq_mgmt_request('GET', host, path, user, password, port, timeout)


def rabbitmq_queue_publish(host: str = 'rabbitmq', vhost: str = '', routing_key: str = '',
                           body: str = '', exchange: str = '', properties: dict = None,
                           user: str = RABBITMQ_MGMT_AUTH[0], password: str = RABBITMQ_MGMT_AUTH[1],
                           port: int = RABBITMQ_MGMT_PORT, timeout: int = RABBITMQ_MGMT_TIMEOUT,
                           message_properties: dict = None) -> tuple:
    """Опубликовать сообщение в очередь management-API: POST /api/queues/{vhost}/{rout}/publish.

    «Publish в очередь, когда консюмер-сервис (swarrot) не поднят» — тот же
    publish, что `swarrot:publish`, но из Python без PHP-консоли; `delivery_mode=2`
    = persistent.

    Args:
        host: хост менеджмент-API.
        vhost: виртуальный хост ('payment_gateway'); '' — дефолтный '/'.
        routing_key: ключ роутинга (= имя очереди для default exchange).
        body: текст сообщения; dict — кладется JSON-телом.
        exchange: имя exchange ('' — default).
        properties: поля publish ({'delivery_mode': 2, ...}); поверх дефолтов.
        user, password: креды.
        port: порт management-API.
        timeout: таймаут коннекта, сек.
        message_properties: то же, что properties (для совместимости вызовов).

    Returns:
        (resp, None) либо (None, 'текст ошибки').
    """
    if not routing_key:
        return None, 'нужен routing_key (для очереди = её имя); пусто — не публиковать'
    payload = {'vhost': vhost or '/', 'routing_key': routing_key,
               'payload': json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body,
               'payload_encoding': 'string',
               'properties': {'delivery_mode': 2, 'content_type': 'application/json',
                              'contentType': 'application/json', 'persistent': 2,
                              **(properties or {}), **(message_properties or {})}}
    if exchange:
        payload['exchange'] = exchange
    return rabbitmq_mgmt_request('POST', host,
                                 f'queues/{urllib.parse.quote(vhost or "/", safe="")}'
                                 f'/{urllib.parse.quote(routing_key, safe="")}/publish',
                                 user, password, port, timeout, payload)


def rabbitmq_queue_declare(host: str = 'rabbitmq', vhost: str = '', queue: str = '',
                           durable: bool = True, user: str = RABBITMQ_MGMT_AUTH[0],
                           password: str = RABBITMQ_MGMT_AUTH[1], port: int = RABBITMQ_MGMT_PORT,
                           timeout: int = RABBITMQ_MGMT_TIMEOUT, arguments: dict = None,
                           auto_delete: bool = False) -> tuple:
    """Создать/проверить очередь: PUT /api/queues/{vhost}/{queue}/{args} (durable=x).

    «Очередь `payment_gateway.delayed-payment` с аргументами x-delayed-*
    создаётся `swarrot:exchange:create`/`vhost:mapping:create`» — тот же путь
    через management-API, когда PHP-консоль недоступна (контейнер не поднят).

    Args:
        host: хост менеджмент-API.
        vhost: виртуальный хост.
        queue: имя очереди.
        durable: durable=true в query.
        user, password: креды.
        port: порт management-API.
        timeout: таймаут коннекта, сек.
        arguments: аргументы x-arguments (напр. {'x-delayed-type': 'delayed-message'}).
        auto_delete: auto-delete для топиков.

    Returns:
        (True, None) либо (False, 'текст ошибки').
    """
    if not queue:
        return None, 'нужен queue (имя очереди)'
    path = f'queues/{urllib.parse.quote(vhost or "/", safe="")}/{urllib.parse.quote(queue, safe="")}'
    if arguments is not None:
        path += '/' + urllib.parse.quote(json.dumps(arguments), safe='')
    path += f'?durable={"true" if durable else "false"}'
    if auto_delete:
        path += '&auto-delete=true'
    _, err = rabbitmq_mgmt_request('PUT', host, path, user, password, port, timeout, body={})
    return (err is None), err


def rabbitmq_queue_purge(host: str = 'rabbitmq', vhost: str = '', queue: str = '',
                         user: str = RABBITMQ_MGMT_AUTH[0], password: str = RABBITMQ_MGMT_AUTH[1],
                         port: int = RABBITMQ_MGMT_PORT, timeout: int = RABBITMQ_MGMT_TIMEOUT) -> tuple:
    """Полный дрейн очереди: DELETE /api/queues/{vhost}/{queue}/contents.

    Args:
        host: хост менеджмент-API.
        vhost: виртуальный хост.
        queue: имя очереди.
        user, password: креды.
        port: порт management-API.
        timeout: таймаут коннекта, сек.

    Returns:
        (True, None) либо (False, 'текст ошибки').
    """
    if not queue:
        return False, 'нужен queue (имя очереди)'
    path = f'queues/{urllib.parse.quote(vhost or "/", safe="")}/{urllib.parse.quote(queue, safe="")}'
    _, err = rabbitmq_mgmt_request('DELETE', host, path + '/contents', user, password, port, timeout)
    return (err is None), err


def rabbitmq_queue_depth(host: str = 'rabbitmq', vhost: str = '', queue: str = '',
                         user: str = RABBITMQ_MGMT_AUTH[0], password: str = RABBITMQ_MGMT_AUTH[1],
                         port: int = RABBITMQ_MGMT_PORT, timeout: int = RABBITMQ_MGMT_TIMEOUT) -> tuple:
    """Счетчики очереди: GET /api/queues/{vhost}/{queue} — messages_ready, consumers.

    Args:
        host: хост менеджмент-API.
        vhost: виртуальный хост.
        queue: имя очереди.
        user, password: креды.
        port: порт management-API.
        timeout: таймаут коннекта, сек.

    Returns:
        ({'messages', 'messages_ready', 'consumers', ...}, None) либо (None, 'ошибка').
    """
    if not queue:
        return None, 'нужен queue (имя очереди)'
    path = f'queues/{urllib.parse.quote(vhost or "/", safe="")}/{urllib.parse.quote(queue, safe="")}'
    data, err = rabbitmq_mgmt_request('GET', host, path, user, password, port, timeout)
    if err:
        return None, err
    if not isinstance(data, dict) or 'message_stats' not in data:
        return None, f'брокер ответил не очередью: {str(data)[:120]}'
    return {'messages': data.get('messages'), 'messages_ready': data.get('messages_ready'),
            'messages_unacknowledged': data.get('messages_unacknowledged'),
            'consumers': data.get('consumers'), 'consumer_details': data.get('consumer_details'),
            'state': data.get('state')}, None


def rabbitmq_vhost_create(host: str = 'rabbitmq', vhost: str = '', user: str = RABBITMQ_MGMT_AUTH[0],
                          password: str = RABBITMQ_MGMT_AUTH[1], port: int = RABBITMQ_MGMT_PORT,
                          timeout: int = RABBITMQ_MGMT_TIMEOUT) -> tuple:
    """Создать vhost и дать юзеру полный контроль: PUT /api/vhosts + /api/permissions.

    «Vhost payment_gateway/payment_gateway_test; `vhost:mapping:create -Hrabbitmq
    -uguest -pguest`» — тот же путь без PHP-консоли.

    Args:
        host: хост менеджмент-API.
        vhost: имя создаваемого vhost.
        user, password: креды.
        port: порт management-API.
        timeout: таймаут коннекта, сек.

    Returns:
        (True, None) либо (False, 'текст ошибки').
    """
    if not vhost:
        return False, 'нужен vhost (имя создаваемого vhost)'
    quote_vhost = urllib.parse.quote(vhost, safe='')
    _, err = rabbitmq_mgmt_request('PUT', host, f'vhosts/{quote_vhost}', user, password,
                                   port, timeout, body={})
    if err:
        return False, err
    _, err = rabbitmq_mgmt_request(
        'PUT', host, f'permissions/{quote_vhost}/{urllib.parse.quote(user, safe="")}',
        user, password, port, timeout,
        body={'user': user, 'vhost': vhost, 'permissions': {'configure': '.*', 'write': '.*',
                                                           'read': '.*'}})
    return (err is None), err


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('overview', 'queues', 'depth', 'publish', 'declare', 'purge',
                                    'vhost-create'), help='что показать')
    _P.add_argument('--host', default='rabbitmq', help='хост менеджмент-API')
    _P.add_argument('--vhost', default='', help='виртуальный хост')
    _P.add_argument('--queue', default='', help='имя очереди')
    _P.add_argument('--routing-key', default='', help='ключ роутинга (publish)')
    _C = _P.parse_args()
    if _C.cmd in ('overview', 'queues'):
        _out, _err = rabbitmq_mgmt_get(_C.host, {'overview': 'overview',
                                                 'queues': 'queues?pagination=false'}[_C.cmd])
    elif _C.cmd == 'depth':
        _out, _err = rabbitmq_queue_depth(_C.host, _C.vhost, _C.queue)
    elif _C.cmd == 'publish':
        _out, _err = rabbitmq_queue_publish(_C.host, _C.vhost, _C.routing_key or _C.queue, '')
    elif _C.cmd == 'declare':
        _out, _err = rabbitmq_queue_declare(_C.host, _C.vhost, _C.queue)
    elif _C.cmd == 'purge':
        _out, _err = rabbitmq_queue_purge(_C.host, _C.vhost, _C.queue)
    else:
        _out, _err = rabbitmq_vhost_create(_C.host, _C.vhost)
    if _err:
        print(f'ошибка: {_err}', file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(_out, ensure_ascii=False, indent=1, default=str)[:4000] if _out is not None
          else 'ok')
    raise SystemExit(0)

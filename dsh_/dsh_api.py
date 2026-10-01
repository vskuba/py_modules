"""Позвать метод хоста DeepSeek Harness: cookie печётся сама, конверт тоже.

Хост с 0.1.5 пускает `/api/*` **только по cookie** — петля больше не
достаточная защита. В проекте уже есть свой клиент (`src/dsh/dsh_api.py`),
но он знает пуль, `.env` и ответ на вопросы хоста — из другого проекта за
ним не потянуться. Наружу всегда нужны были только две вещи: «жив ли хост»
и «позвать метод», — и обе требуют лишь секрета подписи, который лежит на
той же машине в `<DSH_HOME>/.credentials.yaml`, записью
`client-connection/browser-session`.

Cookie привязана к авторитету (`Host:` запроса): одна на 8790 не подойдёт
хосту на 8890, поэтому адрес — единственный параметр и печём её на каждый
запрос (один sha256 и один hmac — дешевле, чем следить за протуханием).
Срок заведомо час: хост отвергает cookie дольше своей `cookieMaxAgeDays`,
а час короче любого допустимого значения.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid

# Адрес хоста по умолчанию: `dsh --profile web` жёстко слушает петлю на 8790,
# и обходить это нельзя — пропуск у хоста один на всех.
DSH_API_ADDR_DEFAULT = '127.0.0.1:8790'

# Каталог хоста; `DSH_HOME` сильнее умолчания — на нём же стоит песочница.
DSH_API_HOME_DEFAULT = '~/.dsh'

# Запись секрета в `.credentials.yaml`: `<scope>/<id>` плагина, его заведшего.
DSH_API_RECORD = 'client-connection/browser-session'

# Срок cookie: с запасом меньше минимума, который хост позволяет настроить,
# и с запасом больше любого одного запроса.
DSH_API_TTL = 3600

DSH_API_TIMEOUT = 10.0


class DshApiError(RuntimeError):
    """Хост не ответил, ответил веткой ошибки или не пустил (401)."""


def dsh_api_call(method: str, args: dict = None, *, base: str = DSH_API_ADDR_DEFAULT,
                 timeout: float = DSH_API_TIMEOUT):
    """Позвать метод хоста и вернуть полезное значение из-под конверта.

    Args:
        method: `<область>/<метод>` — ровно как в реестре хоста
            (`session/list`, `continuous-agent.list`).
        args: аргументы по именам параметров обработчика
            (`{"request": {...}}` и родня); пусто — метод без параметров.
            Пустой `{}` обязателен там, где у метода нет параметров:
            без конверта хост отвечает `gateway/arguments-invalid`.
        base: `host:port` хоста; с ним подписана cookie, тот же `Host:`
            и уйдёт.
        timeout: потолок секунд на запрос.

    Returns:
        `result.value` как есть: словарь, список, число, `True` или `None`.

    Raises:
        DshApiError: ветка ошибки в конверте, 401 (нечем подписывать или
            секрет чужой), хост не отвечает, ответ не конверт.

    ⚠ Методы без параметров требуют пустой `args` — так хост отличает
    «не передали» от «передано пусто»; `None` и `{}` здесь не одно и то же
    на транспортном уровне, наружу оба сворачиваются в `{}`.
    """
    envelope = {'type': 'client-request', 'rpcId': str(uuid.uuid4()),
                'method': method, 'payload': {'args': args if args is not None else {}}}
    url = 'http://' + base + '/api/' + method
    request = urllib.request.Request(url, data=json.dumps(envelope).encode('utf-8'),
                                     method='POST', headers=_headers(base))
    body = _send(request, timeout)
    result = body.get('result') if isinstance(body, dict) else None
    if not isinstance(result, dict):
        raise DshApiError(f'ответ без result: {str(body)[:200]}')
    if result.get('ok'):
        return result.get('value')
    error = result.get('error') or {}
    raise DshApiError(f"{error.get('code', 'internal')}: "
                      f"{error.get('message', 'без описания')}")


def dsh_api_get(path: str, *, base: str = DSH_API_ADDR_DEFAULT,
                query: str = '', timeout: float = DSH_API_TIMEOUT):
    """Прочитать GET-ручку хоста (плагины, `/api/<домен>.<route>`) разбором JSON.

    Маршруты плагинов (`bell.state`, `service.state`) отдают обычный JSON без
    конверта client-request — здесь он и не наращивается, что вернула ручка,
    то и приходит.

    Args:
        path: путь без ведущего `/api/` (`'bell.state?fresh=1'` допустим
            со своей query).
        query: отдельная query-строка; склеивается с path через `?`/`&`.
        base: `host:port` хоста.
        timeout: потолок секунд.

    Returns:
        Разобранный JSON ручки (обычно словарь); `None` — ручка ответила
            пустым телом.

    Raises:
        DshApiError: 401/404/500 или неразобранный JSON.
    """
    url = 'http://' + base + '/api/' + path
    if query:
        url += ('&' if '?' in url else '?') + query
    request = urllib.request.Request(url, headers=_headers(base))
    raw = _send_raw(request, timeout)
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as error:
        raise DshApiError(f'ручка ответила не JSON: {raw[:200]!r}') from error


def dsh_api_alive(*, base: str = DSH_API_ADDR_DEFAULT, timeout: float = 1.5) -> bool:
    """Отвечает ли хост вообще — секрета для этого вопроса не нужно.

    Смысл вопроса ровно в «жив ли процесс и слушает ли порт», поэтому
    спрашивается открытый корень, а не метод под cookie: мёртвый хост и
    живой-но-без-секрета — разные ответы, и путать их сторожу нельзя.

    Args:
        base: `host:port` хоста.
        timeout: потолок секунд на попытку.

    Returns:
        True — пришёл любой ответ HTTP (даже 401); False — не соединился,
        истёк срок, разговора нет.
    """
    try:
        urllib.request.urlopen('http://' + base + '/', timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


# ── детали реализации ──

# Секрет живёт на диске и меняется только вместе с файлом: держим прочитанное
# при себе, свежесть сверяем по времени правки.
_SECRET_CACHE = {}


def _headers(base: str) -> dict:
    cookie = _cookie(base)
    return {'Content-Type': 'application/json', 'Cookie': cookie}


def _send(request, timeout: float) -> dict:
    raw = _send_raw(request, timeout)
    try:
        return json.loads(raw)
    except ValueError as error:
        raise DshApiError(f'ответ не разбирается как JSON: {raw[:200]!r}') from error


def _send_raw(request, timeout: float) -> str:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        raw = error.read().decode('utf-8', 'replace')
        if error.code in (401, 403):
            raise DshApiError(f'unauthorized (HTTP {error.code}): нечем подписать '
                              f'пропуск — секрет хоста не тот или cookie истекла') from error
        raise DshApiError(f'HTTP {error.code}: {raw[:200]}') from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DshApiError(f'хост не ответил: {error}') from error


def _cookie(base: str) -> str:
    """Готовое значение `Cookie` для этого авторитета: `v1.<payload>.<подпись>`."""
    secret = _secret_read()
    now = int(time.time() * 1000)
    payload = {'version': 1, 'authority': base,
               'issuedAt': now, 'expiresAt': now + DSH_API_TTL * 1000}
    body = _b64u(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    sign = _b64u(hmac.new(secret, body.encode('ascii'), hashlib.sha256).digest())
    name = 'dsh-auth-' + _b64u(hashlib.sha256(base.encode('utf-8')).digest())
    return f'{name}=v1.{body}.{sign}'


def _secret_read() -> bytes:
    home = os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT)
    path = os.path.join(home, '.credentials.yaml')
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError as error:
        raise DshApiError(f'{path}: {error.strerror} — хост здесь не поднимался?') from error
    cached = _SECRET_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    import yaml                              # тяжёлая половина ядра не тянет
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            document = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as error:
        raise DshApiError(f'{path}: не читается ({error})') from error
    record = ((document.get('records') or {}).get(DSH_API_RECORD) or {})
    raw = (record.get('payload') or {}).get('secret')
    if not isinstance(raw, str):
        raise DshApiError(f'{path}: нет записи {DSH_API_RECORD}')
    secret = _b64u_decode(raw)
    if len(secret) != 32:
        raise DshApiError(f'{path}: секрет {DSH_API_RECORD} испорчен')
    _SECRET_CACHE[path] = (stamp, secret)
    return secret


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64u_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + '=' * ((4 - len(text) % 4) % 4))
    except (ValueError, TypeError):
        return b''


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='хост harness: alive — жив ли, call — метод-конверт, '
                    'get — ручка плагина; вывод — JSON.')
    ap.add_argument('verb', choices=('alive', 'call', 'get'), help='что сделать')
    ap.add_argument('what', nargs='?', default='',
                    help='call: область/метод; get: путь ручки; alive: не нужен')
    ap.add_argument('--args', default='', metavar='{json}',
                    help='call: аргументы JSON')
    ap.add_argument('--base', default=DSH_API_ADDR_DEFAULT, help='host:port хоста')
    ap.add_argument('--timeout', type=float, default=DSH_API_TIMEOUT)
    ns = ap.parse_args()
    try:
        if ns.verb == 'alive':
            ok = dsh_api_alive(base=ns.base, timeout=min(ns.timeout, 1.5))
            print('alive' if ok else 'dead')
            raise SystemExit(0 if ok else 1)
        if ns.verb == 'get':
            value = dsh_api_get(ns.what, base=ns.base, timeout=ns.timeout)
        else:
            value = dsh_api_call(ns.what, json.loads(ns.args) if ns.args else None,
                                 base=ns.base, timeout=ns.timeout)
        print(json.dumps(value, ensure_ascii=False, indent=1))
    except (DshApiError, ValueError) as error:
        print(str(error))
        raise SystemExit(1)

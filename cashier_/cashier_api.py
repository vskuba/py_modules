"""
Внешний API бренда (single payment API): вызов, схема поля и кадр «запрос → ответ».

Фасад `cashier-service-api-gateway` — то, чем бренд дергает Cashier руками: OAuth-токен,
`POST /api/v1/<операция>/payment-methods`, затем `…/payment-process`. Для агента это
обычно два конца задачи: проверить, что новый провайдер действительно появился в списке
методов и что платёж проходит, и показать это Хозяину кадром. Между ними — рутина,
которая каждый раз съедает вызовы: токен просрочен, тело ушло form-ом (сервер отвечает
`invalid_grant`), обязательные поля угаданы на глаз, а карточка для тикета собирается
вручную из двух JSON.

Здесь вызов — одна строка, токен берётся у `cashier_brand` и освежается сам, поля
сверяются со схемой, которую сервер отдаёт тем же HTTP, а карточка — `http_shot`.

    python -m cashier_.cashier_api methods deposit --account WC1 --env stage
    python -m cashier_.cashier_api process refund --account WC1 --body @refund.json
    python -m cashier_.cashier_api schema refund/payment --env stage
    python -m cashier_.cashier_api shot deposit/payment-process --account WC1 --out /tmp/a.png

⚠ Вызов создаёт **живые транзакции** в общей среде: на stage — тестовыми картами
провайдера и своим CID, на prod — только по прямой просьбе Хозяина.
⚠ Ответ и тело запроса на печать уходят замазанными (`http_shot_mask`); полный токен —
в файле 0600, не в выводе.
"""
import argparse
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from pathlib import Path

from cashier_.cashier_brand import cashier_brand_token
from cashier_.cashier_env import cashier_env_headers, cashier_env_host
from http_.http_shot import http_shot_html, http_shot_mask, http_shot_render

# Операции внешнего API: у каждой своя пара payment-methods / payment-process.
CASHIER_API_OPERATIONS = ('deposit', 'payout', 'refund')

# Конечные точки операции; methods отдаёт обязательные поля, process делает платёж.
CASHIER_API_ENDPOINTS = ('payment-methods', 'payment-process')

# Кто подписывает callback: `hash_hmac(algorithm, body, signature_key)` заголовком X-Signature.
CASHIER_API_SIGNATURE_ALGORITHM = 'sha512'


def cashier_api_call(path: str, body: dict | None = None, account: str = '', env: str = 'stage',
                     method: str = 'POST', token: str = '', root: str = '', store: str = '',
                     idempotency: str = '', timeout: float = 60, fresh: bool = False) -> dict:
    """
    Один вызов внешнего API бренда: путь, тело, Bearer — и разобранный ответ.

    Токен берётся у `cashier_brand_token` (свежий или из кэша), если не передали свой.
    Ошибки HTTP (422, 400) — не исключение, а ответ: API так описывает непрошедшую
    валидацию, и её надо видеть в `response`, а не ловить.

    Args:
        path: `/api/v1/refund/payment-methods` или `refund/payment-methods` — префикс добавится.
        body: тело запроса; `None` — `{}`.
        account: имя или uuid бренда; по нему берётся токен.
        env: среда; `method` — глагол (по умолчанию POST, GET тоже годится).
        token: свой Bearer — когда бренд задан токеном, а не учёткой.
        idempotency: `X-Idempotency-Key`, чтобы повтор не сделал второй платёж.
        fresh: токен взять заново, не глядя в кэш (сменились креды бренда).
        timeout: секунд ожидания; платёж у провайдера идёт секунды, не миллисекунды.

    Returns:
        `{'ok', 'status', 'url', 'method', 'request', 'response', 'text', 'headers'}`;
        `ok` — 2xx, `response` — разобранный JSON (или None), `text` — сырой ответ.

    Raises:
        RuntimeError: хост не ответил или креды бренда не взялись.
    """
    url = cashier_env_host(env, 'api') + _cashier_api_path(path)
    bearer = token or (cashier_brand_token(account, env=env, root=root, store=store, fresh=fresh)
                       ['access_token'] if account else '')
    headers = cashier_env_headers({'Authorization': f'Bearer {bearer}'} if bearer else None)
    if idempotency:
        headers['X-Idempotency-Key'] = idempotency
    data = None if method.upper() == 'GET' else json.dumps(body if body is not None else {}).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text, status, head = response.read().decode(errors='replace'), response.status, dict(response.headers)
    except urllib.error.HTTPError as err:
        text, status, head = err.read().decode(errors='replace'), err.code, dict(err.headers)
    except urllib.error.URLError as err:
        raise RuntimeError(f'{url} не отвечает: {err.reason} (VPN? верна ли среда {env}?)')
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    return {'ok': 200 <= status < 300, 'status': status, 'url': url, 'method': method.upper(),
            'request': body if body is not None else {}, 'response': parsed, 'text': text, 'headers': head}


def cashier_api_methods(operation: str = 'deposit', account: str = '', env: str = 'stage',
                        root: str = '', store: str = '', body: dict | None = None,
                        fresh: bool = False) -> dict:
    """
    Список методов оплаты операции — и обязательные поля каждого из них.

    Первым делом по новому провайдеру: если его `alias` не появился здесь, до process
    дело не дойдёт, и причина в настройке бренда (экземпляр, каскад, visibility), а не в API.
    Ответ — тот же JSON, что отдаёт бренд-интегратору Cashier (`cashier_api_call`).
    """
    _cashier_api_operation(operation)
    return cashier_api_call(f'/api/v1/{operation}/payment-methods', body or {}, account, env,
                            root=root, store=store, fresh=fresh)


def cashier_api_process(operation: str = 'deposit', body: dict | None = None, account: str = '',
                        env: str = 'stage', root: str = '', store: str = '', idempotency: str = '',
                        fresh: bool = False) -> dict:
    """
    Платёж операцией (депозит, выплата, возврат) — тот же путь, что у бренда.

    Тело не выдумывается: поля диктует `cashier_api_schema(f'{operation}/payment')` и
    обязательные поля из `payment-methods`. Возврат — то же самое с `operation='refund'`;
    ответ `PENDING` нормален, финальный статус приходит callback-ом на `notification_url`.

    Raises:
        RuntimeError: бренд не отдал токен или хост не ответил.
    """
    _cashier_api_operation(operation)
    return cashier_api_call(f'/api/v1/{operation}/payment-process', body, account, env, root=root,
                            store=store, idempotency=idempotency, fresh=fresh)


def cashier_api_schema(name: str = 'refund/payment', env: str = 'stage', timeout: float = 20) -> dict:
    """
    Json-schema запроса (или одного поля) — первоисточник обязательных полей, а не догадка.

    Адреса `https://api.<среда>ipaygateway.com/json-schema/v1/…`: `<операция>/paymentRequest`,
    `<операция>/paymentResponse`, `<операция>/methodsRequest`, `requiredField/<поле>`
    (список полей отдаёт `payment-methods`). Окончание `Request` подставится само — по
    короткому `refund/payment` сервер 404-ит, а искать руками нечем.

    Raises:
        RuntimeError: схемы нет ни под одним из испробованных имён, либо хост не ответил.
    """
    base = name.strip('/')
    tails = [base] if '://' in base else (
        [base] if base.endswith(('Request', 'Response')) else [f'{base}Request', base])
    tried, last = [], None
    for tail in tails:
        url = tail if '://' in tail else f"{cashier_env_host(env, 'api')}/json-schema/v1/{tail}.json"
        tried.append(url)
        request = urllib.request.Request(url, headers=cashier_env_headers())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text, status = response.read().decode(errors='replace'), response.status
            break
        except urllib.error.HTTPError as err:
            last = f'{url} — HTTP {err.code} {err.read().decode(errors="replace")[:120]}'
        except urllib.error.URLError as err:
            raise RuntimeError(f'{url} не отвечает: {err.reason} (VPN? верна ли среда {env}?)')
    else:
        raise RuntimeError(f'схемы {name!r} нет; пробовал {", ".join(tried)}; {last}')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f'{url} ответил не-JSON (HTTP {status}): {text[:200]}')


def cashier_api_signature(body: str | bytes, signature_key: str,
                          algorithm: str = CASHIER_API_SIGNATURE_ALGORITHM) -> str:
    """
    Подпись callback-а (`X-Signature`) — та же, что проверяет бренд: hmac по сырому телу.

    Считается по **сырым байтам** запроса: пересобрать JSON и подписать его — другая подпись,
    и бренд ответит 400. Ключ лежит в `account.signature_key` сервиса (`cashier_brand_endpoints`).
    """
    raw = body.encode() if isinstance(body, str) else body
    return hmac.new(signature_key.encode(), raw, getattr(hashlib, algorithm)).hexdigest()


def cashier_api_shot(result: dict, out: str, title: str = '', note: str = '',
                     collapse: tuple = ()) -> str:
    """
    Кадр «запрос → ответ» из уже сделанного вызова — картинка в тикет или отчёт.

    Секреты замазываются сами (`http_shot_mask`), токен на карточке — точки. Возвращает
    путь к PNG; для Jira его потом кладёт `jira_api comment … --attach`.
    """
    request = http_shot_mask(result.get('request'), collapse=set(collapse))
    response = http_shot_mask(result.get('response'), collapse=set(collapse))
    page = http_shot_html(title or f"{result.get('method', 'POST')} {result.get('url', '')}",
                          result.get('method', 'POST'), result.get('url', ''), request,
                          result.get('status'), response, note=note)
    return http_shot_render(page, out)


# ------------------------------------------------------------------- приватные функции

def _cashier_api_operation(operation: str) -> str:
    if (operation or '') not in CASHIER_API_OPERATIONS:
        raise ValueError(f'операции {operation!r} нет: бывает {", ".join(CASHIER_API_OPERATIONS)}')
    return operation


def _cashier_api_target(target: str, endpoint: str = 'payment-methods') -> str:
    """Операция («refund») — в путь, путь («/api/v1/…») — как есть: CLI один на обоих."""
    if (target or '') in CASHIER_API_OPERATIONS:
        return f'/api/v1/{target}/{endpoint}'
    return target


def _cashier_api_path(path: str) -> str:
    text = (path or '').strip()
    if not text.startswith('/'):
        text = '/api/v1/' + text.lstrip('/')
    return text


def _cashier_api_body(raw: str) -> dict:
    if not raw:
        return {}
    text = Path(raw[1:]).read_text() if raw.startswith('@') else raw
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'тело не JSON: {exc}')


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description='внешний API бренда: методы, платёж, схема, кадр')
    _P.add_argument('command', nargs='?', default='methods',
                    choices=('methods', 'process', 'schema', 'signature', 'shot'))
    _P.add_argument('target', nargs='?', default='deposit',
                    help='операция (deposit/payout/refund), путь вызова или имя схемы')
    _P.add_argument('--account', default='', help='имя или uuid бренда')
    _P.add_argument('--env', default='stage')
    _P.add_argument('--root', default='', help='checkout, чей .env.local читать')
    _P.add_argument('--store', default='', help='где лежит кэш токена')
    _P.add_argument('--body', default='', help='тело JSON или @файл')
    _P.add_argument('--key', default='', help='signature_key (для signature)')
    _P.add_argument('--file', default='', help='сырое тело callback-а (для signature)')
    _P.add_argument('--out', default='', help='куда положить PNG (для shot)')
    _P.add_argument('--endpoint', default='payment-methods', choices=CASHIER_API_ENDPOINTS,
                    help='конечная точка операции (для shot)')
    _P.add_argument('--fresh', action='store_true', help='токен взять заново')
    _P.add_argument('--note', default='', help='подпись под кадром')
    _C = _P.parse_args()
    try:
        if _C.command == 'schema':
            print(json.dumps(cashier_api_schema(_C.target, env=_C.env), ensure_ascii=False, indent=2))
            raise SystemExit(0)
        if _C.command == 'signature':
            raw = Path(_C.file).read_bytes() if _C.file else b''
            if not raw:
                raise SystemExit('ошибка: тело callback-а берут из файла — важен каждый байт (--file)')
            print(cashier_api_signature(raw, _C.key))
            raise SystemExit(0)
        endpoint = {'methods': 'payment-methods', 'process': 'payment-process'}.get(
            _C.command, _C.endpoint)
        got = cashier_api_call(_cashier_api_target(_C.target, endpoint), _cashier_api_body(_C.body),
                              _C.account, _C.env, root=_C.root, store=_C.store, fresh=_C.fresh)
        if _C.command == 'shot':
            print(cashier_api_shot(got, _C.out or f'{_C.target}-{endpoint}.png', note=_C.note))
        else:
            print(json.dumps(http_shot_mask(got['response']), ensure_ascii=False, indent=2))
        raise SystemExit(0 if got['ok'] else 1)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f'ошибка: {exc}')

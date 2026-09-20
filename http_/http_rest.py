"""REST-вызов, умятый ошибки: авторизация угадана, схема вычитана из валидации.

Чужие agent-API встречают чужим заголовком ключа (`x-api-key` не каждый догадается),
400-ми с zod-подобными `path[]/expected/received`, из которых можно было бы собрать
скелет тела, и openapi на неизвестной подпапке. Каждый раз это один и тот же цикл:
ткнуть пустым телом — прочесть ошибку — повторить. Здесь цикл стал функцией: вызов
пробует разумные заголовки сам, а ошибка валидации возвращается недостающими
полями, а не сырым телом.
"""

import json as _json
import urllib.request, urllib.error

_HTTP_REST_SCHEMA_CANDIDATES = ('/api/openapi.json', '/openapi.json',
                                '/api/docs-json', '/openapi.json')
_HTTP_REST_AUTH_TRIES = ('x-api-key', 'Authorization: Bearer')
# хост → заголовок, которым ключ принят; чтобы не гадать на каждый вызов
_http_rest_tried: dict = {}


def http_rest_call(url: str, method: str = 'GET', body=None, auth=None,
                   headers: dict | None = None) -> dict:
    """Один вызов к чужому REST: ключ угадан, ошибка валидации — полем «missing».

    Args:
        url: полный адрес ручки.
        method: GET/POST/PATCH.
        body: dict — уйдёт как JSON целиком.
        auth: ключ/токен; заголовок подберётся (x-api-key, затем Bearer) и
            запомнится на хост.
        headers: дополнительные заголовки как есть.

    Returns:
        {'ok': True, 'status', 'body'} на успех; {'ok': False, 'status',
        'message', 'missing': {путь.поля: ожидание}} на 400-ку валидации
        (zod-подобные issues переводятся в скелет тела).
    """
    h = {'content-type': 'application/json', 'accept': 'application/json'}
    h.update(headers or {})
    host = url.split('/')[2]
    order = [_http_rest_tried.get(host)] + [t for t in _HTTP_REST_AUTH_TRIES
                                           if t != _http_rest_tried.get(host)]
    last: dict = {}
    for hname in [o for o in order if o]:
        hh = dict(h)
        if auth:
            if hname.startswith('Authorization'):
                hh['Authorization'] = f'Bearer {auth}'
            else:
                hh[hname] = auth
        try:
            req = urllib.request.Request(url, data=_json.dumps(body).encode() if body is not None else None,
                                         headers=hh, method=method)
            with urllib.request.urlopen(req, timeout=20) as r:
                _http_rest_tried[host] = hname if auth else None
                return {'ok': True, 'status': r.status, 'body': _try(r.read())}
        except urllib.error.HTTPError as e:
            raw = e.read()[:64_000].decode(errors='replace')
            if e.code == 401:
                last = {'ok': False, 'status': 401,
                        'message': f'ключ не принят заголовком {hname!r}: {str(_try(raw))[:120]}',
                        'missing': {}}
                continue                     # заголовок не тот — пробуем следующий
            msg, miss = _shape(_try(raw))
            return {'ok': False, 'status': e.code, 'message': msg,
                    'missing': miss, 'raw': raw[:400]}
        except Exception as e:
            return {'ok': False, 'status': 0, 'message': str(e), 'missing': {}}
    return last or {'ok': False, 'status': 0, 'message': 'ни один заголовок не перебран',
                    'missing': {}}


def http_rest_schema(url: str) -> dict:
    """Схема API: openapi найден или кандидаты прозондированы + список ручек.

    Returns:
        {'found': '/api/openapi.json' или None, 'routes': ['GET /api/…', …]}.
    """
    root = url.split('/')[0] + '//' + url.split('/')[2] if '://' in url else url
    out = {'found': None, 'routes': []}
    for c in _HTTP_REST_SCHEMA_CANDIDATES:
        r = http_rest_call(root + c)
        if r['ok'] and isinstance(r['body'], dict) and r['body'].get('paths'):
            out['found'] = root + c
            out['routes'] = [f'{m.upper()} {p}' for p, ms in r['body']['paths'].items()
                             for m in ms]
            return out
    return out


# ─── приватное ─────────────────────────────────────────────────────────────

def _try(raw):
    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _shape(j):
    err = j.get('error', j) if isinstance(j, dict) else {}
    if isinstance(err, dict) and isinstance(err.get('issues'), list):
        return err.get('message', ''), {str(i.get('path')): i.get('expected')
                                        for i in err['issues']}
    return (err.get('message', '') if isinstance(err, dict) else str(j)[:200]), {}

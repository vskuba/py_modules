"""Состояние панели точкой: ручка API → поле ответа, без curl и json-разбора.

Между «что в DOM» (`web_drive_page`) и «что в базе» (`state_watch`) — середина:
«что отвечает ручка». Раньше это был каждый раз однострочник
`curl -s -b /tmp/cj … | python3 -c "json.load…"` с jar-файлом куки, спором про
`result` и полем по пути — четыре одинаковых куска на каждый вопрос. Здесь
точка: адрес, поля, учётка из `.env` — значение наружу.

Ответ панелей этого каркаса всегда конверт `{'result': …}` — конверт снимается
молча, человек спрашивает про содержимое.
"""
import httpx


def web_state(url, *, params=None, method='GET', json_body=None,
              login=None, login_url='/auth/login') -> object:
    """Спросить живую панель и вернуть поле ответа (из-под конверта `result`).

    Args:
        url: полный адрес ручки (обычно `http://localhost:<порт>/api/...`).
        params: query-параметры (`{'persona': 'diana'}`).
        method: 'GET' | 'POST' | 'PUT' | 'DELETE'.
        json_body: тело JSON для пишущих ручек.
        login: {'username','password'} или None — тогда `ADMIN_USERNAME`/
            `ADMIN_PASSWORD` из `.env`; False — ручка без входа.
        login_url: адрес ручки входа панели.

    Returns:
        значение `result` ответа (или весь ответ, если конверта нет).

    Raises:
        RuntimeError: вход не прошёл или ручка ответила не 2xx — словами, как
        ответила.
    """
    if login is None:
        from config.config import config_get
        login = {'username': config_get('ADMIN_USERNAME', ''),
                 'password': config_get('ADMIN_PASSWORD', '')}
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        if login:
            r = client.post(_origin(url) + login_url,
                            headers={'Content-Type': 'application/json'},
                            content=_json(login))
            if r.status_code >= 400:
                raise RuntimeError(f'вход не вышел: {r.status_code} {r.text}')
        r = client.request(method, url, params=params, content=_json(json_body)
                           if json_body is not None else None,
                           headers={'Content-Type': 'application/json'}
                           if json_body is not None else None)
        if r.status_code >= 400:
            raise RuntimeError(f'{method} {url} ответил {r.status_code}: '
                              f'{r.text[:300]}')
        body = r.json()
    return body.get('result', body) if isinstance(body, dict) else body


def _json(value) -> bytes:
    import json
    return json.dumps(value, ensure_ascii=False).encode()


def _origin(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return f'{p.scheme}://{p.netloc}'


if __name__ == '__main__':
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(
        description='Спросить живую панель и вернуть поле ответа из-под '
                    'конверта result (учётка — из .env проекта).')
    ap.add_argument('url', help='полный адрес ручки')
    ap.add_argument('--params', action='append', default=[],
                    metavar='ключ=значение', help='query-параметр, повторять')
    ap.add_argument('--method', default='GET')
    ap.add_argument('--body', default='', help='тело JSON для пишущих ручек')
    ns = ap.parse_args()
    print(_json.dumps(web_state(
        ns.url, params=dict(p.split('=', 1) for p in ns.params) or None,
        method=ns.method, json_body=ns.body or None), ensure_ascii=False))

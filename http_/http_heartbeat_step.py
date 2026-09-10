"""Один шаг настройки heartbeat: собрать запрос, послать, прочесть ответ.

Отдельно от `http_heartbeat.py`, потому что ответственность другая: там — что
значит «сессия жива», здесь — как разговаривать по описанию из настройки. Шаг
описывается словарём:

    {"path": "/services/beat", "method": "POST", "body": "mode=online",
     "headers": {...}, "no_redirect": true,
     "auth_field": "auth" | "ok_mark": "//OK"}

Обязателен только `path`; остальное — умолчания и приметы живого входа.
"""

import httpx


async def http_heartbeat_step_call(client: httpx.AsyncClient, config: dict, step,
                                   cookies: dict):
    """Шаг настройки: собрать адрес, послать, вернуть ответ. `None` — шага нет.

    ⚠ **Cookie и заголовки идут параметрами запроса, а не состоянием клиента.**
    Клиент общий на все сессии (см. `http_heartbeat.py`), и запись в него
    означала бы, что соседняя сессия уходит под чужим входом.

    Ошибку не глотаем: решать, что делать с недоступным сайтом, — дело
    вызывающего. Удару она стоит одного пропуска, проверке — вердикта `error`, и
    смешивать эти два решения здесь нельзя.
    """
    if not step:
        return None

    base = str((config or {}).get('base') or '').rstrip('/')
    url = base + '/' + str(step.get('path') or '').lstrip('/')
    headers = {**((config or {}).get('headers') or {}), **(step.get('headers') or {})}

    # Метод выводится из тела: есть что послать — `POST`. Так настройка короче на
    # строку в самом частом случае, а сказать явно можно всегда.
    method = str(step.get('method')
                 or ('GET' if step.get('body') is None else 'POST')).upper()

    # ⚠ **`no_redirect` — единственный способ отличить гостя на многих сайтах.**
    # Сайт не отвечает выкинутой сессии `401`, а молча уводит на форму входа, и
    # клиент, идущий по переадресации, получает те же `200`, что и живая сессия.
    # С этим ключом гость даёт `302`, а вошедший — `200`, и разница видна сразу.
    kwargs = {'cookies': cookies, 'headers': headers}
    if step.get('no_redirect'):
        kwargs['follow_redirects'] = False

    if method == 'GET':
        return await client.get(url, **kwargs)

    return await client.request(method, url, content=step.get('body'), **kwargs)


def http_heartbeat_step_ok(step: dict, response) -> bool:
    """Признал ли сайт вход в этом ответе.

    Три приметы, потому что три вида ответов:

      `auth_field` — поле в JSON: сайт прямо говорит, кто пришёл;
      `ok_mark`    — метка в тексте, когда ответ не JSON вовсе;
      ни того ни другого — «раз ответил, значит жив»: годится там, где сайт
      гостю отказывает кодом.

    ⚠ Умолчание годится только вместе с `no_redirect`. Без него сайт, уводящий
    гостя на форму входа, отвечает `200` и ему — и выглядит живым всегда. Именно
    поэтому годным считается `2xx`, а не «меньше 400»: переадресация это `3xx`, и
    засчитывать её значило бы принимать «иди авторизуйся» за «ты авторизован».
    """
    if response is None:
        return False

    field = str((step or {}).get('auth_field') or '')
    if field:
        return bool(http_heartbeat_step_json(response).get(field))

    mark = str((step or {}).get('ok_mark') or '')
    if mark:
        return mark in (response.text or '')

    return 200 <= response.status_code < 300


def http_heartbeat_step_json(response) -> dict:
    """Тело ответа словарём. Не JSON или не объект — пустой словарь.

    Пустой, а не `None`: у вызывающего тогда один разбор на все случаи, и `.get()`
    не приходится оборачивать проверкой.
    """
    if response is None:
        return {}

    try:
        data = response.json()
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}

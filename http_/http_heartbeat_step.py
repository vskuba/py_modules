"""Один шаг настройки heartbeat: собрать запрос, послать, прочесть ответ.

Отдельно от `http_heartbeat.py`, потому что ответственность другая: там — что
значит «сессия жива», здесь — как разговаривать по описанию из настройки. Шаг
описывается словарём:

    {"path": "/services/beat", "method": "POST", "body": "mode=online",
     "headers": {...}, "no_redirect": true, "base": "https://chat.site.example",
     "auth_field": "auth" | "ok_mark": "//OK"}

Обязателен только `path`; остальное — умолчания и приметы живого входа.
`base` у шага — хост, отличный от корневого: у сайтов чат-сервер живёт на своём
поддомене, а присутствие считается именно там.

## Метки в адресе и теле

`path` и `body` могут нести `{метку}` — движок подставляет её по сессии:

  `{cookie:NAME}` — значение cookie `NAME`;
  `{ИМЯ}`       — значение из `values` вызывающего, чем он различает свои
                    сессии (номер учётки на сайте, логин — движок их не толкует).

Метки нужны там, где сайт ждёт про пользователя в адресе, а не в cookie.
Метку, которую подставить нечем, движок **не превращает в запрос с фигурными
скобками внутри**: сайт прочтёт их как битый адрес и ответит молча, и искать
такую беду по пустому `False` дороже явного отказа.
"""

import httpx
import re

# `{метка}`: имя — латиница, цифры, `_`, `.`; префикс `cookie:` — разбор cookie.
HTTP_HEARTBEAT_TOKEN = re.compile(r'\{([A-Za-z0-9_.]+(?::[A-Za-z0-9_.]+)?)\}')


async def http_heartbeat_step_call(client: httpx.AsyncClient, config: dict, step,
                                   cookies: dict, values: dict = None):
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

    # Хост шагом: корень у сайта один, а чат-сервер присутствия живёт на поддомене.
    base = str(step.get('base') or (config or {}).get('base') or '').rstrip('/')
    url = base + '/' + _fill(str(step.get('path') or ''), cookies, values).lstrip('/')
    headers = {**((config or {}).get('headers') or {}), **(step.get('headers') or {})}
    body = (_fill(str(step['body']), cookies, values) if step.get('body') is not None
            else None)

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

    return await client.request(method, url, content=body, **kwargs)


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


# ── Приватное ────────────────────────────────────────────────────────────────

def _fill(text: str, cookies: dict, values: dict) -> str:
    """Подставить `{метки}` из cookie и значений вызывающего.

    Непогашенная метка — отказ словами, а не запрос с `{...}` внутри адреса:
    сайт прочтёт их как битый путь и ответит молча, и искать такую беду по
    пустому `False` дороже явной строки.
    """
    if not text or '{' not in text:
        return text

    def one(match):
        token = match.group(1)
        cookie = token.startswith('cookie:')
        name = token.split(':', 1)[1] if cookie else token
        found = (cookies or {}) if cookie else (values or {})
        if name in found:
            return str(found[name])

        raise ValueError(f'в шаге heartbeat нечем подставить метку {{{token}}}')

    return HTTP_HEARTBEAT_TOKEN.sub(one, text)

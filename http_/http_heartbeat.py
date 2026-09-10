"""HTTP-heartbeat: держим чужую веб-сессию живой запросом по таймеру, без браузера.

Многие сайты считают человека «в сети», пока его открытая страница шлёт им
периодический запрос. Идёт запрос — человек в списке онлайн; перестал — через
минуту-две пропадает оттуда. Держать ради этого вкладку браузера незачем: браузер
нужен один раз, чтобы **войти**, а дальше хватает cookie и одного запроса раз в
полминуты.

Цена разницы решающая, и она замерена:

| Что держим | На одну сессию | Тысяча сессий |
|------------|----------------|---------------|
| вкладка Chromium | ~143 МБ | 143 ГБ — невозможно |
| свой `httpx.AsyncClient` на сессию | ~707 КБ | 690 МБ — дорого и незачем |
| **общий клиент, cookie на запрос** | **~4,7 КБ** | **4,5 МБ** |

⚠ **Отсюда главное правило модуля: клиент общий, cookie передаются на каждый
запрос.** Клиент на сессию кажется естественным — у каждой свои cookie, — но
каждый несёт свой пул соединений: тысяча клиентов это тысяча пулов и тысяча
рукопожатий TLS на круг. Общий клиент переиспользует соединения к тому же хосту,
и от сессии остаётся только словарь cookie.

⚠ **Потолок здесь не наш, а чужого сайта.** Памяти тысяча сессий стоит копейки,
но раз в минуту это 17 запросов в секунду с одного адреса, и сайт вправе счесть
это нападением. Частоту и `HTTP_HEARTBEAT_PARALLEL` подбирают под сайт, а не под нас.

## Что описывает настройка

Модуль не знает ни одного сайта — куда идти, говорит настройка. Формат:

    {
      "base": "https://site.example",
      "headers": {"Referer": "https://site.example/lk"},
      "beat":   {"path": "/services/beat", "body": "mode=online"},
      "alive":  [{"path": "/services/state", "body": "mode=update", "auth_field": "auth"}],
      "unread": {"path": "/api/stats", "method": "GET", "field": "unreadMail"}
    }

`beat` — то самое «я здесь». `alive` — по чему видно, что вход ещё жив: сайт
отвечает `200` и гостю, поэтому нужна примета (их разбирает `http_heartbeat_step.py`).
`unread` — необязательный счётчик, если сайт отдаёт его попутно.

`alive` может быть списком: у одного сайта разные кабинеты отвечают разными
модулями и в разных форматах. Подошла любая проверка — вход жив.

## Чего модуль не делает

Не входит и не выходит: вход — дело браузера и сценариев, они у каждого проекта
свои. Сюда приходят готовые cookie, а откуда они взялись — не его забота. Не
ходит в базу, не знает расписаний и учёток: он про запрос и ответ.
"""

import asyncio
import time

import httpx

from logging_.logging_ import logger_info
from http_.http_heartbeat_step import (http_heartbeat_step_call,
                                       http_heartbeat_step_json,
                                       http_heartbeat_step_ok)

# Браузерный `User-Agent`: сайт отдаёт свои ручки тому, кто похож на его страницу.
HTTP_HEARTBEAT_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36')

# Ответ приходит за 0,4–2 секунды; дольше десяти ждать нечего — круг повторится.
HTTP_HEARTBEAT_TIMEOUT = 10.0

# Ошибка сайта уходит наверх строкой, а не стеком: её читают глазами.
HTTP_HEARTBEAT_ERROR_MAX = 500

# Сколько запросов идёт разом. ⚠ Предел про **чужой** сайт, а не про нас: ровный
# поток он переносит, залп из тысячи — нет.
HTTP_HEARTBEAT_PARALLEL = 20


def http_heartbeat_ready(config: dict) -> bool:
    """Настроен ли heartbeat у этого сайта.

    Без настройки честнее молчать, чем показывать «не в сети»: это разные вещи, и
    вторая соврала бы про сайт, которому мы просто ещё не объяснили, куда идти.
    """
    data = config or {}

    return bool(data.get('base') and (data.get('beat') or data.get('alive')))


def http_heartbeat_client() -> httpx.AsyncClient:
    """Общий клиент под все сессии. Закрывать вызывающему.

    ⚠ Он **без cookie**: они у каждой сессии свои и уходят с запросом. Клиент
    здесь ради переиспользования соединений, а не ради состояния.
    """
    return httpx.AsyncClient(
        timeout=HTTP_HEARTBEAT_TIMEOUT, follow_redirects=True,
        headers={'User-Agent': HTTP_HEARTBEAT_UA},
        limits=httpx.Limits(max_connections=HTTP_HEARTBEAT_PARALLEL * 2,
                            max_keepalive_connections=HTTP_HEARTBEAT_PARALLEL))


async def http_heartbeat_send(client: httpx.AsyncClient, cookies: dict,
                         config: dict) -> bool:
    """Один удар — «я на сайте». Ничего не проверяет и не читает.

    Отдельно от `http_heartbeat_check`, потому что цена разная: здесь один запрос на
    сессию, там до трёх. Тысяча сессий раз в полминуты — 33 запроса в секунду
    против сотни.

    Returns:
        bool: ушёл ли запрос. `False` — либо нечем (нет настройки или cookie),
        либо сайт не ответил; разбираться в причине удару не положено, для этого
        есть `http_heartbeat_check`.
    """
    step = (config or {}).get('beat')
    if not step or not cookies:
        return False

    try:
        await http_heartbeat_step_call(client, config, step, cookies)
    except Exception as e:
        logger_info(f'[http_heartbeat] удар не прошёл: {e}')

        return False

    return True


async def http_heartbeat_send_many(client: httpx.AsyncClient, sessions: dict,
                                   config: dict) -> dict:
    """Удар по многим сессиям разом: `{ключ: cookie}` → `{ключ: дошло ли}`.

    ⚠ **Одним клиентом и с оглядкой на сайт.** Ради этого модуль и написан:
    тысяча сессий проходит одним пулом соединений, но не одним залпом —
    `HTTP_HEARTBEAT_PARALLEL` держит поток ровным. Залп из тысячи запросов сайт
    прочтёт как нападение, ровные семнадцать в секунду — как обычную посещаемость.

    ⚠ **Сорвавшийся удар не роняет остальные.** У каждой сессии свой ответ, и
    одна протухшая не должна лишать сайта всех прочих; поэтому исключение здесь
    превращается в `False`, а не летит наверх.

    Ключ — что угодно, чем вызывающий различает свои сессии: модуль его не
    толкует и возвращает как есть.
    """
    limit = asyncio.Semaphore(HTTP_HEARTBEAT_PARALLEL)

    async def one(key, cookies):
        async with limit:
            try:
                return key, await http_heartbeat_send(client, cookies, config)
            except Exception as e:
                logger_info(f'[http_heartbeat] сессия {key}: {e}')

                return key, False

    done = await asyncio.gather(*(one(key, cookies)
                                  for key, cookies in (sessions or {}).items()))

    return dict(done)


async def http_heartbeat_check(client: httpx.AsyncClient, cookies: dict,
                          config: dict) -> dict:
    """Полная проверка: отметиться, убедиться, что вход жив, снять счётчик.

    Returns:
        dict: `status` — `online` | `offline` | `error`; `unread` — непрочитанных
        (`-1` — не спрашивали, и это не то же, что ноль); `error` — причина при
        `error`; `ms` — сколько занял заход.

    ⚠ `offline` и `error` — разные исходы. Первый значит «сайт нас не признал,
    нужен повторный вход», второй — «до сайта не достучались». Слив их в один, мы
    гоняли бы вход браузером из-за чужой сетевой аварии.
    """
    if not cookies:
        return _result('offline', error='вход не выполнен: нет cookie')

    started = time.monotonic()
    try:
        # Удар первым: он и есть «я на сайте». Ответ у него обычно пустой, поэтому
        # смотрим только на то, что запрос состоялся.
        if (config or {}).get('beat'):
            await http_heartbeat_step_call(client, config, config['beat'], cookies)

        if not await _alive(client, config, cookies):
            return _result('offline', ms=_ms(started),
                           error='сайт не признал вход: нужен повторный логин')

        unread = await _unread(client, config, cookies)
    except Exception as e:
        return _result('error', ms=_ms(started), error=f'{type(e).__name__}: {e}')

    return _result('online', unread=unread, ms=_ms(started))


def http_heartbeat_cookies(storage_state, base: str) -> dict:
    """Cookie нужного хоста из `storage_state` браузера — «имя → значение».

    ⚠ Отбор по домену обязателен. В состоянии лежат и чужие cookie (аналитика,
    CDN), а уехав на сайт скопом, они выдают клиент, который «слишком много
    знает»: у настоящей страницы этих имён в запросе нет.
    """
    host = str(base or '').split('//')[-1].split('/')[0].lower()
    if not host:
        return {}

    # Домен второго уровня: сайт кладёт cookie то на `site.com`, то на `www.site.com`.
    root = '.'.join(host.split('.')[-2:])

    found = {}
    for cookie in (storage_state or {}).get('cookies') or []:
        domain = str(cookie.get('domain') or '').lstrip('.').lower()
        if domain and root not in domain:
            continue

        found[str(cookie.get('name'))] = str(cookie.get('value'))

    return found


# ── Приватное ────────────────────────────────────────────────────────────────

async def _alive(client: httpx.AsyncClient, config: dict, cookies: dict) -> bool:
    """Жив ли вход. Проверки нет — верим удару и считаем, что жив.

    Проверок может быть несколько: у одного сайта разные кабинеты отвечают
    разными модулями. Подошла любая — вход жив; отказ у остальных ничего не
    значит, и до конца списка мы идём именно поэтому.
    """
    steps = (config or {}).get('alive')
    if not steps:
        return True

    for step in steps if isinstance(steps, list) else [steps]:
        if http_heartbeat_step_ok(step, await http_heartbeat_step_call(client, config, step,
                                                             cookies)):
            return True

    return False


async def _unread(client: httpx.AsyncClient, config: dict, cookies: dict) -> int:
    """Счётчик непрочитанных. Не настроен — `-1`: «не спрашивали» ≠ «ноль»."""
    step = (config or {}).get('unread')
    if not step:
        return -1

    response = await http_heartbeat_step_call(client, config, step, cookies)
    value = http_heartbeat_step_json(response).get(str(step.get('field') or 'unread'))

    return int(value) if str(value).isdigit() else -1


def _result(status: str, unread: int = -1, error: str = '', ms: int = 0) -> dict:
    """Ответ одной формы на все исходы: у вызывающего один разбор."""
    return {'status': status, 'unread': int(unread),
            'error': str(error)[:HTTP_HEARTBEAT_ERROR_MAX], 'ms': int(ms)}


def _ms(started: float) -> int:
    """Сколько прошло, в миллисекундах."""
    return int((time.monotonic() - started) * 1000)

"""REST-API Cronicle: расписание, включатель события и история прогонов.

Планировщик у нас уже есть — ходит он к нам (`POST /cronjob`), а мы к нему до
сих пор не ходили: посмотреть, что запланировано, и выключить событие можно было
только в его собственной панели, на другом порту и под другим входом. Этот
модуль закрывает обратную сторону.

## Ключ

Cronicle пускает по `X-API-Key`. Ключ заводится в его панели (Admin → API Keys)
и кладётся в `.env` как `CRONICLE_API_KEY`; адрес — `CRONICLE_URL`, по умолчанию
имя сервиса в сети compose.

⚠ Ключ идёт **заголовком**, а не параметром адреса: `?api_key=…` оседает в
логах прокси и в истории запросов целиком, заголовок — нет.

⚠⚠ Галочка у ключа нужна ровно одна — **`edit_events`**, и только ради
включателя. Сверено по исходникам Cronicle (`lib/api/event.js`): `get_schedule`
и `get_history` проверяют лишь действующего пользователя
(`requireValidUser`), привилегии у них нет вовсе; `update_event` требует
`edit_events`.

⚠ Галочки «get_schedule» в Cronicle **не существует** — искать её в списке
бесполезно. Ключ без `edit_events` при этом читает расписание молча и верно, а
на переключателе отвечает отказом: выглядит это как сломанная кнопка, а не как
недостающее право.

## Чего здесь нет

Заведения и удаления событий. Расписание установки — предмет, который правят
осознанно и редко, и делать это мимоходом из чужой панели незачем: испорченное
расписание тихо не проявляется, оно проявляется не случившимся прогоном.
"""

import datetime

import httpx

from config.config import config_get

# Куда ходить. Имя сервиса в сети compose: снаружи порт у каждой установки свой,
# изнутри он один и тот же.
CRONICLE_API_URL = 'http://cronicle:3012'

# Потолок одного запроса. Cronicle отвечает из памяти и быстро; длинные
# ожидания здесь означают, что он лёг, — и страница должна сказать это сразу.
CRONICLE_API_TIMEOUT = 8.0

# Сколько прогонов тянем из истории за раз. История общая на все события и
# отсортирована свежим вперёд; сотня покрывает сутки установки с запасом.
CRONICLE_API_HISTORY = 100


def cronicle_api_base() -> str:
    """Адрес Cronicle: `CRONICLE_URL` из окружения или имя сервиса."""
    return str(config_get('CRONICLE_URL') or CRONICLE_API_URL).rstrip('/')


def cronicle_api_key() -> str:
    """Ключ API из окружения; пусто — ключа нет и ходить незачем."""
    return str(config_get('CRONICLE_API_KEY') or '')


async def cronicle_api_schedule(limit: int = 100) -> list:
    """Расписание: события планировщика со своим состоянием включателя.

    Args:
        limit: сколько событий вернуть.

    Returns:
        list: события как их отдаёт Cronicle — `id`, `title`, `enabled`,
        `timing`, `category`, `plugin`, `target`.

    Raises:
        RuntimeError: ключа нет, Cronicle не ответил или ответил отказом.
    """
    answer = await _cronicle_api_call('get', 'get_schedule', {'limit': limit})
    return list(answer.get('rows') or [])


async def cronicle_api_enable(event_id: str, enabled: bool) -> dict:
    """Включить или выключить событие.

    Args:
        event_id: идентификатор события Cronicle.
        enabled: True — включить, False — выключить.

    Returns:
        dict: ответ Cronicle без служебного `code`.

    ⚠ Выключенное событие остаётся в расписании и не запускается — это пауза,
    а не удаление. Идущий прямо сейчас прогон выключатель не останавливает.
    """
    return await _cronicle_api_call('post', 'update_event',
                                    {'id': str(event_id),
                                     'enabled': 1 if enabled else 0})


async def cronicle_api_history(limit: int = CRONICLE_API_HISTORY) -> list:
    """Завершённые прогоны, свежие сверху.

    Args:
        limit: сколько прогонов вернуть.

    Returns:
        list: прогоны — `id`, `event`, `event_title`, `code` (0 — удача),
        `time_start`, `elapsed`, `description`.
    """
    answer = await _cronicle_api_call('get', 'get_history', {'limit': limit})
    return list(answer.get('rows') or [])


def cronicle_api_last_ok_today(history: list, offset_minutes: int = 0) -> dict:
    """Последний удачный прогон **за сегодня** по каждому событию.

    Args:
        history: что вернул `cronicle_api_history`.
        offset_minutes: сдвиг пояса показа относительно UTC — тот же, которым
            установка показывает время людям (настройка `timezone`).

    Returns:
        dict: `event_id` → прогон. Событие без удачного прогона сегодня в
        ответе отсутствует вовсе.

    ⚠⚠ «Сегодня» считается в поясе показа, а не в UTC. Установка с поясом +3
    в полночь по UTC уже три часа как живёт следующим днём: вердикт «сегодня
    прогонов не было» расходился бы с тем, что человек видит на экране.

    ⚠ Прогоны приходят свежими сверху, поэтому первый найденный он и есть
    последний; пересортировывать незачем.
    """
    shift = datetime.timedelta(minutes=offset_minutes)
    today = (datetime.datetime.now(datetime.timezone.utc) + shift).date()

    out = {}
    for row in history:
        event = str(row.get('event') or '')
        if not event or event in out or int(row.get('code') or 0) != 0:
            continue
        started = row.get('time_start')
        if not started:
            continue
        when = (datetime.datetime.fromtimestamp(float(started),
                                                datetime.timezone.utc) + shift)
        if when.date() == today:
            out[event] = row
    return out


def _cronicle_api_port_hint() -> str:
    """Подсказка про порт, когда соединение не встало, — или пусто.

    ⚠⚠ Самая вероятная причина отказа, и она неочевидна: в `CRONICLE_URL`
    написали порт, **опубликованный наружу**, а не внутрисетевой. Снаружи у
    установки он свой (`3013`, чтобы не спорить с туннелем на прод), внутри сети
    compose контейнер слушает `3012` всегда. Сообщение «ConnectError» об этом
    молчит, и искать начинают упавший планировщик, а он жив.
    """
    base = cronicle_api_base()
    if base == CRONICLE_API_URL or ':3012' in base:
        return ''
    return (f' Проверьте порт: внутри сети compose Cronicle слушает 3012, а'
            f' опубликованный наружу порт изнутри не отвечает. Уберите'
            f' CRONICLE_URL — умолчание {CRONICLE_API_URL} верно.')


async def _cronicle_api_call(method: str, path: str, payload: dict) -> dict:
    """Один запрос к Cronicle; отказ — исключением с его же текстом.

    ⚠ Cronicle отвечает **200 даже на отказ**: беда лежит в теле, полем `code`
    (ноль — удача) и `description`. Проверка по HTTP-статусу пропускала бы
    «ключа нет» как успешный пустой ответ.
    """
    key = cronicle_api_key()
    if not key:
        raise RuntimeError('ключ Cronicle не задан: добавьте CRONICLE_API_KEY '
                           'в .env (завести — в панели Cronicle, Admin → API Keys)')

    url = f'{cronicle_api_base()}/api/app/{path}/v1'
    headers = {'X-API-Key': key}
    try:
        async with httpx.AsyncClient(timeout=CRONICLE_API_TIMEOUT) as http:
            if method == 'get':
                answer = await http.get(url, params=payload, headers=headers)
            else:
                answer = await http.post(url, json=payload, headers=headers)
    except httpx.HTTPError as err:
        raise RuntimeError(f'Cronicle недоступен ({cronicle_api_base()}): '
                           f'{type(err).__name__}.{_cronicle_api_port_hint()}') from err

    try:
        body = answer.json()
    except ValueError:
        raise RuntimeError(f'Cronicle ответил не JSON ({answer.status_code})')

    if int(body.get('code') or 0) != 0:
        raise RuntimeError(f"Cronicle отказал: {body.get('description') or body.get('code')}")

    body.pop('code', None)
    return body

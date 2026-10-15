"""Настоящие жилые адреса рядом с точкой — из OpenStreetMap.

Вопрос, на который отвечает модуль: «дай адреса жилых домов в пятистах метрах
отсюда». Он возникает везде, где адрес человека должен быть **правдоподобным
рядом с известной точкой** — отделением перевозчика, офисом, складом.

## ⚠⚠ Зачем настоящий адрес, если можно выдумать

Выдуманный выдаёт себя сразу и по-разному: улицы нет в этом городе; улица есть,
но дома с таким номером нет; дом есть, но это школа. Проверяется это одним
взглядом в карту — и тем, кто проверяет, обычно оказывается не наша сторона.
Адрес из OSM не нуждается в правдоподобии: он правдивый.

## ⚠⚠ Жилые — это перечень, а не `building=yes`

`building=yes` стоит у половины зданий OSM, включая магазины, школы и гаражи, и
взяв его, получаешь «вул. Шкільна, 7» — школу. Берём перечисленные жилые типы
(`GEO_OSM_ADDRESS_LIVING`); их мало, зато каждый — жильё.

## ⚠ Улица приводится к виду, каким её пишут в заказе

OSM хранит «Теремська вулиця» и «вулиця Кармелюка» — родной порядок слов карты.
В заказе пишут «вул. Теремська» и «вул. Кармелюка»: сокращение впереди. Это и
делает `geo_osm_address_street`, иначе адрес выглядит выпиской из базы, а не
строкой, которую набрал человек.

## ⚠ Overpass — чужой бесплатный сервис, и он это помнит

Запросы с пустым `User-Agent` он отклоняет **406**, частые — ставит в очередь
или рвёт. Отсюда обязательный заголовок, щадящий таймаут и второй сервер в
запасе (`GEO_OSM_ADDRESS_SERVERS`). Ходить сюда в цикле на каждый заказ нельзя:
адреса собирают заранее и хранят у себя.
"""

import math
import re

import httpx

# Куда спрашивать. ⚠ По порядку: первый не ответил — идём ко второму. Зеркала
# отвечают одним и тем же, разница только в очереди и настроении.
GEO_OSM_ADDRESS_SERVERS = (
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
)

# ⚠⚠ Без него Overpass отвечает 406 и не объясняет. Поймано живьём.
GEO_OSM_ADDRESS_AGENT = 'geo_osm_address/1.0 (+py_modules)'

# Жилые типы зданий. См. шапку — список закрытый намеренно.
GEO_OSM_ADDRESS_LIVING = (
    'apartments', 'residential', 'house', 'detached', 'semidetached_house',
    'terrace', 'dormitory', 'bungalow',
)

# Сколько ждать ответа: Overpass считает долго, а наш запрос — на километр.
GEO_OSM_ADDRESS_TIMEOUT = 60

# Как далеко от точки искать, когда радиус не задан.
GEO_OSM_ADDRESS_RADIUS = 800

# Сколько адресов брать за раз. ⚠ Не «все»: квартал в городе даёт их тысячами,
# а для выбора случайного хватает сотен.
GEO_OSM_ADDRESS_LIMIT = 300

# Как OSM пишет тип улицы и чем его заменить в заказе.
GEO_OSM_ADDRESS_KINDS = (
    ('вулиця', 'вул.'), ('улица', 'вул.'), ('проспект', 'просп.'),
    ('провулок', 'пров.'), ('переулок', 'пров.'), ('бульвар', 'бульв.'),
    ('площа', 'пл.'), ('площадь', 'пл.'), ('набережна', 'наб.'),
    ('шосе', 'шосе'), ('узвіз', 'узвіз'), ('тупик', 'туп.'),
    ('дорога', 'дорога'), ('алея', 'алея'),
)


async def geo_osm_address_around(lat: float, lon: float, radius: int = 0,
                                 limit: int = 0) -> list:
    """Жилые адреса вокруг точки, ближние первыми.

    Args:
        lat: широта точки.
        lon: долгота.
        radius: радиус поиска в метрах; `0` — `GEO_OSM_ADDRESS_RADIUS`.
        limit: сколько адресов вернуть; `0` — `GEO_OSM_ADDRESS_LIMIT`.

    Returns:
        Список `{'street', 'house', 'postcode', 'city', 'lat', 'lon',
        'metre', 'line'}`, отсортированный по расстоянию. Пусто — вокруг
        точки нет размеченного жилья (село, промзона, белое пятно карты).

    Raises:
        RuntimeError: ни один сервер не ответил.

    ⚠ Расстояние считается ЗДЕСЬ, а не берётся у Overpass: он его не
    возвращает вовсе, а знать его надо — адрес в девятистах метрах от
    отделения правдоподобен, в девяти километрах нет.

    ⚠⚠ Дом без номера пропускается молча. В OSM такие есть (контур здания без
    адреса), и «вул. Садова, None» хуже, чем отсутствие строки.
    """
    radius = int(radius or GEO_OSM_ADDRESS_RADIUS)
    limit = int(limit or GEO_OSM_ADDRESS_LIMIT)

    kinds = '|'.join(GEO_OSM_ADDRESS_LIVING)
    query = (
        '[out:json][timeout:45];('
        f'way["building"~"^({kinds})$"]["addr:housenumber"]["addr:street"]'
        f'(around:{radius},{lat},{lon});'
        f'node["building"~"^({kinds})$"]["addr:housenumber"]["addr:street"]'
        f'(around:{radius},{lat},{lon});'
        f');out center {limit};')

    found = []
    for row in await _osm_ask(query):
        tags = row.get('tags') or {}
        house = str(tags.get('addr:housenumber') or '').strip()
        street = geo_osm_address_street(tags.get('addr:street') or '')
        if not house or not street:
            continue

        spot = row.get('center') or row
        at_lat, at_lon = spot.get('lat'), spot.get('lon')
        if at_lat is None or at_lon is None:
            continue

        found.append({
            'street': street, 'house': house,
            'postcode': str(tags.get('addr:postcode') or '').strip(),
            'city': str(tags.get('addr:city') or '').strip(),
            'lat': float(at_lat), 'lon': float(at_lon),
            'metre': round(geo_osm_address_metre(lat, lon, at_lat, at_lon)),
            'line': f'{street}, {house}',
        })

    found.sort(key=lambda one: one['metre'])

    return found


def geo_osm_address_street(name: str) -> str:
    """Улица в том виде, каким её пишут в заказе: «вул. Теремська».

    ⚠ Тип переносится ВПЕРЁД, а не отбрасывается: «Теремська» без «вул.»
    в адресной строке читается как фамилия, а у площади и переулка с тем же
    названием это и вовсе разные места.

    >>> geo_osm_address_street('Теремська вулиця')
    'вул. Теремська'
    >>> geo_osm_address_street('вулиця Карпа Валовня')
    'вул. Карпа Валовня'
    >>> geo_osm_address_street('Одеське шосе')
    'шосе Одеське'
    >>> geo_osm_address_street('Андріївський узвіз')
    'Андріївський узвіз'
    """
    whole = re.sub(r'\s+', ' ', str(name or '')).strip()
    if not whole:
        return ''

    low = whole.lower()
    for long, short in GEO_OSM_ADDRESS_KINDS:
        if long not in low:
            continue

        # ⚠ «Узвіз» и «алея» в украинском стоят ПОСЛЕ названия («Андріївський
        # узвіз»), и перестановка их портит. Остальные — впереди.
        if short in ('узвіз', 'алея', 'дорога'):
            return whole

        rest = re.sub(rf'\b{long}\b', '', low)
        rest = re.sub(r'\s+', ' ', rest).strip(' ,')
        if not rest:
            return whole

        # Возвращаем кусок ИСХОДНОЙ строки, а не приведённый к нижнему
        # регистру: имена собственные теряют заглавные буквы безвозвратно.
        at = low.find(rest)
        rest = whole[at:at + len(rest)] if at >= 0 else rest

        return f'{short} {rest}'

    return whole


def geo_osm_address_metre(lat1: float, lon1: float,
                          lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками в метрах (формула гаверсинуса).

    ⚠ Не «по дорогам», а по прямой. Для вопроса «это соседний квартал или
    другой конец города» этого достаточно, а маршрутизация стоила бы ещё
    одного чужого сервиса.

    >>> round(geo_osm_address_metre(50.4501, 30.5234, 50.4501, 30.5234))
    0
    >>> 880 < geo_osm_address_metre(50.4501, 30.5234, 50.4581, 30.5234) < 900
    True
    """
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)

    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)

    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


# ── детали реализации ──────────────────────────────────────────────────────

async def _osm_ask(query: str) -> list:
    """Спросить Overpass, перебирая зеркала; вернуть `elements`.

    ⚠ Отказ одного сервера — не отказ источника: зеркала живут по-разному, и
    то, что на первом «слишком много запросов», на втором обычная очередь.
    Молчат оба — вот это отказ, и о нём говорим словами.
    """
    trouble = []
    for where in GEO_OSM_ADDRESS_SERVERS:
        try:
            async with httpx.AsyncClient(
                    timeout=GEO_OSM_ADDRESS_TIMEOUT,
                    headers={'User-Agent': GEO_OSM_ADDRESS_AGENT}) as client:
                answer = await client.post(where, data={'data': query})

            if answer.status_code != 200:
                trouble.append(f'{where}: {answer.status_code}')
                continue

            return answer.json().get('elements') or []
        except (httpx.HTTPError, ValueError) as fault:
            trouble.append(f'{where}: {fault}')

    raise RuntimeError('OpenStreetMap не ответил — ' + '; '.join(trouble))

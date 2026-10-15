"""Адреса целой страны из выгрузки OpenStreetMap — без очереди Overpass.

Overpass отвечает на вопрос «что вокруг этой точки» и берёт за это секунды:
замер показал 35–50 с на запрос у единственного живого зеркала. Пятьдесят
тысяч точек по такой цене — месяц работы. Выгрузка отвечает на другой вопрос —
«все адреса страны разом», — и платится один раз: файл Украины весит 880 МБ и
разбирается минутами.

```python
from geo_.geo_osm_dump import geo_osm_dump_fetch, geo_osm_dump_addresses

path = await geo_osm_dump_fetch('europe/ukraine')
for row in geo_osm_dump_addresses(path):
    ...  # {'street', 'house', 'city', 'postcode', 'lat', 'lon', 'kind'}
```

## ⚠⚠ Вид адреса важнее, чем кажется

В выгрузке три разных вещи с одинаковым `addr:housenumber`:

| `kind` | Что это | Годится в заказ |
|--------|---------|-----------------|
| `live` | здание жилого типа (`apartments`, `house`, …) | да |
| `point` | адресная точка без здания — так размечают дома | да |
| `other` | `building=yes`, магазин, школа, гараж | **нет** |

Разделение сделано ЗДЕСЬ, потому что по тегам его видно, а по готовой строке
«вул. Шкільна, 7» — уже нет. Берущий сам решает, какие виды ему нужны; смешав
их в один список, он однажды пошлёт посылку в школу.

## ⚠ Улица приводится к виду, каким её пишут в заказе

Тем же `geo_osm_address_street`, что и у Overpass: «Теремська вулиця» →
«вул. Теремська». Два источника обязаны давать одинаковые строки, иначе один
и тот же дом ляжет в базу дважды.

## ⚠⚠ Разбор потоковый, и память не растёт

`FileProcessor` читает файл потоком, а функция — генератор: страна целиком
никогда не лежит в памяти. Собирать её в список `list(...)` можно, но это
миллионы словарей; лучше писать пачками прямо из обхода.

⚠ `with_locations()` обязателен: без него у контура здания нет координат
узлов, и центр посчитать нечем. Платится это памятью на индекс узлов —
полгигабайта на Украину.

## ⚠ Свежесть

Geofabrik обновляет выгрузки ежедневно. `geo_osm_dump_fetch` качает файл
заново, только если локальный старше `fresh_days` — перекачивать 880 МБ ради
суточной разницы незачем.
"""

import time
from pathlib import Path

import httpx

from geo_.geo_osm_address import GEO_OSM_ADDRESS_LIVING, geo_osm_address_street

# Откуда берутся выгрузки. ⚠ Geofabrik, а не planet.osm: тот весит 80 ГБ, и
# страна из него вырезается дольше, чем качается готовая.
GEO_OSM_DUMP_BASE = 'https://download.geofabrik.de'

# Сколько дней выгрузка считается свежей. ⚠ Неделя, а не сутки: адреса домов
# меняются медленно, а 880 МБ каждый день — это чужой трафик впустую.
GEO_OSM_DUMP_FRESH_DAYS = 7

# Сколько ждать скачивания, секунды. ⚠ Щедро: 880 МБ на медленном канале идут
# четверть часа, и обрыв на двенадцатой минуте означает начать сначала.
GEO_OSM_DUMP_TIMEOUT = 3600

# Размер куска при скачивании. Мегабайт — компромисс между числом записей на
# диск и памятью под буфер.
GEO_OSM_DUMP_CHUNK = 1024 * 1024


async def geo_osm_dump_fetch(region: str, to: str = '',
                             fresh_days: int = 0) -> str:
    """Скачать выгрузку региона с Geofabrik; вернуть путь к файлу.

    Args:
        region: путь региона у Geofabrik без расширения — `europe/ukraine`,
            `europe/poland`, `europe/andorra`.
        to: куда положить; пусто — рядом, по имени региона.
        fresh_days: сколько дней местный файл считается свежим; `0` —
            `GEO_OSM_DUMP_FRESH_DAYS`.

    Returns:
        Путь к файлу `.osm.pbf`.

    Raises:
        RuntimeError: источник ответил не 200 или оборвал передачу.

    ⚠⚠ Свежий местный файл НЕ перекачивается. Выгрузка весит сотни мегабайт, и
    повторная закачка ради суточной разницы в адресах — чужой трафик, за
    который платит тот, кто раздаёт карту бесплатно.

    ⚠ Файл пишется во временный и переименовывается в конце: оборванная
    закачка иначе оставляет огрызок, который при следующем запуске выглядит
    как готовый и разбирается с ошибкой посреди страны.
    """
    name = region.strip('/').split('/')[-1]
    path = Path(to) if to else Path(f'{name}-latest.osm.pbf')
    fresh_days = int(fresh_days or GEO_OSM_DUMP_FRESH_DAYS)

    if path.exists() and path.stat().st_size > 0:
        age = (time.time() - path.stat().st_mtime) / 86400
        if age < fresh_days:
            return str(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    draft = path.with_suffix(path.suffix + '.part')
    url = f'{GEO_OSM_DUMP_BASE}/{region.strip("/")}-latest.osm.pbf'

    try:
        async with httpx.AsyncClient(
                timeout=GEO_OSM_DUMP_TIMEOUT, follow_redirects=True) as client:
            async with client.stream('GET', url) as answer:
                if answer.status_code != 200:
                    raise RuntimeError(
                        f'Geofabrik ответил {answer.status_code} на {url}')

                with draft.open('wb') as file:
                    async for chunk in answer.aiter_bytes(GEO_OSM_DUMP_CHUNK):
                        file.write(chunk)
    except httpx.HTTPError as fault:
        draft.unlink(missing_ok=True)

        raise RuntimeError(f'выгрузка не скачалась: {fault}') from fault

    draft.replace(path)

    return str(path)


def geo_osm_dump_addresses(path: str, kinds: tuple = ()):
    """Адреса из выгрузки — генератором, страна в память не грузится.

    Args:
        path: файл `.osm.pbf`.
        kinds: какие виды отдавать (`live`, `point`, `other`); пусто — все.

    Yields:
        `{'street', 'house', 'city', 'postcode', 'lat', 'lon', 'kind'}`.

    Raises:
        RuntimeError: не установлен `osmium` либо файл не читается.

    ⚠⚠ Виды разделены намеренно (см. шапку): `other` — это `building=yes`,
    магазины и школы, и адрес «вул. Шкільна, 7» из них неотличим от жилого,
    пока теги рядом. Фильтровать надо здесь, а не потом.

    ⚠ `osmium` импортируется ЛЕНИВО: пакет весит мегабайты и нужен одной этой
    функции, а модуль рядом (`geo_osm_address`) обходится без него.
    """
    try:
        import osmium
    except ImportError as fault:
        raise RuntimeError(
            'для разбора выгрузки нужен osmium: pip install osmium') from fault

    want = set(kinds or ())
    flow = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY) \
        .with_locations() \
        .with_filter(osmium.filter.KeyFilter('addr:housenumber'))

    for obj in flow:
        tags = obj.tags
        street = geo_osm_address_street(tags.get('addr:street') or '')
        house = str(tags.get('addr:housenumber') or '').strip()
        if not street or not house:
            continue

        spot = _dump_spot(obj)
        if spot is None:
            continue

        kind = _dump_kind(tags.get('building') or '')
        if want and kind not in want:
            continue

        yield {'street': street, 'house': house,
               'city': str(tags.get('addr:city') or '').strip(),
               'postcode': str(tags.get('addr:postcode') or '').strip(),
               'lat': spot[0], 'lon': spot[1], 'kind': kind}


# ── детали реализации ──────────────────────────────────────────────────────

def _dump_kind(building: str) -> str:
    """Что это: жильё, адресная точка или прочее. См. таблицу в шапке."""
    if building in GEO_OSM_ADDRESS_LIVING:
        return 'live'

    return 'point' if not building else 'other'


def _dump_spot(obj):
    """Координаты объекта: у точки свои, у контура — средние по узлам.

    ⚠ Именно среднее по узлам, а не первый узел: у длинного дома первый угол
    отстоит от середины на полсотни метров, и расстояние до отделения
    считалось бы по углу.

    ⚠ Узел без координат пропускается: в урезанной выгрузке (регион вырезан по
    границе) часть узлов контура лежит за её краем, и `nd.location` у них
    невалиден. Контур без единого годного узла отдаёт `None`.
    """
    if obj.is_node():
        return (obj.location.lat, obj.location.lon)

    points = [(nd.lat, nd.lon) for nd in obj.nodes if nd.location.valid()]
    if not points:
        return None

    return (sum(one[0] for one in points) / len(points),
            sum(one[1] for one in points) / len(points))

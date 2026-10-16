"""Хранилище сценариев и их прогон со стороны проекта: найти, сохранить, выполнить.

Контейнер (`browser_scenario_run`) сценариев не хранит и в журнал не пишет — он
отвечает трассой тому, кто попросил. Хранением, журналом и меткой состояния
занимается этот модуль: второй потребитель базы дал бы контейнеру связность там,
где он её специально не имеет — упавшая база не должна ронять живые сессии.

Здесь же — слои значений при прогоне: переменные сайта снизу, значения самого
сценария поверх, переданное на запуск — всего. Побеждает более частное; пустые
переданные значения не стирают сохранённых.
"""

import base64
import json
import pathlib
import re
import time

from browser_.browser_scenario import (BROWSER_SCENARIO_PACE_MAX_MS,
                                       BROWSER_SCENARIO_PACE_MIN_MS,
                                       BrowserScenario,
                                       browser_scenario_variables_used)
from browser_.browser_upstream import (browser_upstream_request,
                                       BROWSER_UPSTREAM_SCENARIO_TIMEOUT)
from browser_.browser_site_variable import browser_site_variable_values
from image_.image_film import image_film_gif
from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_get_db_async
from project_.project_ import project_root

# Сколько жить найденному сценариванию в памяти процесса: исполнитель зовёт его на
# каждую карточку, а правка сценария — редкость. После сохранения кэш всё равно
# выбрасывается сразу, TTL — лишь страховка для правки, сделанной мимо модуля.
BROWSER_SCENARIO_CACHE_TTL_SEC = 600.0

# Найденные сценарии: имя → (строка, момент нахождения). Имя уникально в таблице,
# потому и ключа хватает одного.
_CACHE: dict[str, tuple] = {}


async def browser_scenario_save(name: str, steps: list, site_id: int = 0, goal: str = '',
                                start_url: str = '', variables: dict | None = None,
                                status: str = 'draft', scenario_id: int = 0,
                                pace_min_ms: int | None = None,
                                pace_max_ms: int | None = None) -> int:
    """Завести или переписать сценарий; возвращает его номер.

    Шаги проходят проверку схемой контейнера здесь, при сохранении: ошибку лучше
    увидеть сейчас, а не в первом прогоне. Секретов в `variables` нет — значения
    по умолчанию едут в журнал открытыми; секреты живут у сайта
    (`browser_site_variable`).

    `pace_min_ms`/`pace_max_ms` — темп: пауза перед каждым шагом, случайная в
    этих пределах (`browser_scenario_pace`). `None` — оставить как есть у
    существующего и взять умолчание слоя у нового.

    ⚠ Перевёрнутые пределы выпрямляются молча: это опечатка в форме, а не
    повод не дать человеку сохранить сценарий.

    Raises:
        ValueError: шаги не прошли схему, имя занято другим сценарием или статусу
            такой не положен.
    """
    scenario = _scenario_check(name, goal, steps)
    pace = _pace_values(pace_min_ms, pace_max_ms)
    async with mysql_get_db_async() as db:
        if scenario_id:
            await db.execute('SELECT id FROM browser_scenario WHERE id = %s', (int(scenario_id),))
            if not await db.fetchone():
                raise KeyError('сценарий не найден')
            await db.execute(
                'UPDATE browser_scenario'
                '   SET site_id = %s, name = %s, goal = %s, start_url = %s,'
                '       steps = %s, variables = %s, status = %s,'
                '       pace_min_ms = %s, pace_max_ms = %s WHERE id = %s',
                (site_id or None, name, goal, start_url,
                 json.dumps([s.model_dump() for s in scenario.steps], ensure_ascii=False),
                 json.dumps(variables or {}, ensure_ascii=False), status,
                 pace[0], pace[1], int(scenario_id)))

            browser_scenario_forget(name)

            return int(scenario_id)

        await db.execute(
            'SELECT 1 FROM browser_scenario WHERE name = %s LIMIT 1', (name,))
        if await db.fetchone():
            raise ValueError(f'сценарий с именем «{name}» уже есть')
        await db.execute(
            'INSERT INTO browser_scenario (site_id, name, goal, start_url, steps,'
            '  variables, status, pace_min_ms, pace_max_ms) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (site_id or None, name, goal, start_url,
             json.dumps([s.model_dump() for s in scenario.steps], ensure_ascii=False),
             json.dumps(variables or {}, ensure_ascii=False), status,
             pace[0], pace[1]))
        browser_scenario_forget(name)

        return int(db.lastrowid)


async def browser_scenario_list(site_id: int = 0) -> list:
    """Сценарии сайта, свежие сверху; пустой сайт — все.

    Шаги и значения возвращаются разобранными: страница правит их как массив и
    словарь, а не как JSON-строку, и исполнитель читает их же.
    """
    where, params = ('WHERE site_id = %s', [int(site_id)]) if site_id else ('', [])

    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT id, site_id, name, goal, start_url, status, steps, variables,'
            '       pace_min_ms, pace_max_ms, created_at, updated_at '
            f'FROM browser_scenario {where} ORDER BY id DESC', params)
        rows = await db.fetchall() or []

    items = []
    for row in rows:
        row = dict(row)
        for column in ('steps', 'variables'):
            row[column] = _json_load(row.get(column), [] if column == 'steps' else {})
        items.append(row)

    return items


async def browser_scenario_get(scenario_id: int) -> dict:
    """Сценарий целиком — шаги и имена переменных, которые он ждёт.

    Raises:
        KeyError: сценария с таким номером нет — иначе правка несуществующего
            молча ничего бы не делала.
    """
    async with mysql_get_db_async() as db:
        await db.execute('SELECT * FROM browser_scenario WHERE id = %s', (int(scenario_id),))
        row = await db.fetchone()

    return _scenario_loaded(row)


async def browser_scenario_find(site_id: int, name: str) -> dict:
    """Сценарий сайта по имени — то, чем карточка связана с обработкой.

    Именем, а не номером: карточка помнит имя, как помнит адрес площадки, — и по
    нему же сценарий зовут из задач и расписания. Чужой сайт — всё равно что
    нет такого: сценарий принадлежит своему сайту.

    Raises:
        KeyError: сценария с таким именем нет.
    """
    key = str(name or '')
    hit = _CACHE.get(key)
    if hit is None or time.monotonic() - hit[1] >= BROWSER_SCENARIO_CACHE_TTL_SEC:
        async with mysql_get_db_async() as db:
            await db.execute('SELECT * FROM browser_scenario WHERE name = %s', (key,))
            hit = (await db.fetchone(), time.monotonic())
        _CACHE[key] = hit

    row = dict(hit[0]) if hit[0] else {}
    if not row:
        raise KeyError('сценарий не найден')
    if int(row.get('site_id') or 0) != int(site_id):
        raise KeyError(f'сценарий «{name}» принадлежит другому сайту')

    return _scenario_loaded(row)


def browser_scenario_forget(name: str = '') -> None:
    """Забыть кэш найденных сценариев: весь — без имени, один — по имени."""
    if name:
        _CACHE.pop(str(name), None)
    else:
        _CACHE.clear()


async def browser_scenario_remove(scenario_id: int) -> None:
    """Удалить сценарий вместе с его журналом (каскадом по внешнему ключу)."""
    async with mysql_get_db_async() as db:
        await db.execute('SELECT name FROM browser_scenario WHERE id = %s', (int(scenario_id),))
        row = await db.fetchone()
        if row:
            await db.execute('DELETE FROM browser_scenario WHERE id = %s', (int(scenario_id),))
            browser_scenario_forget(row['name'])


async def browser_scenario_runs(scenario_id: int = 0, limit: int = 50,
                                offset: int = 0, site_id: int = 0) -> list:
    """Журнал прогонов, свежие сверху: статус, шаг, ошибка, трасса, плёнка.

    Args:
        scenario_id: чьи прогоны; ноль — всех сценариев.
        limit, offset: окно выборки.
        site_id: только сценарии этой площадки; ноль — любой.

    ⚠⚠ Трасса и собранное приходят целиком, и на длинном сценарии это десятки
    килобайт на строку. Списком их читают редко (историю смотрят плёнкой), но
    отдельного «лёгкого» запроса здесь нет намеренно: две выборки за одним
    столом расходятся составом полей, и потребитель однажды получит не то,
    что ждал.

    ⚠ Прогоны ВСЕХ сценариев (`scenario_id = 0`) нужны истории: человек
    смотрит, что происходило на площадке, а не перебирает сценарии по одному.
    """
    where, args = [], []
    if scenario_id:
        where.append('r.scenario_id = %s')
        args.append(int(scenario_id))
    if site_id:
        where.append('s.site_id = %s')
        args.append(int(site_id))

    clause = f' WHERE {" AND ".join(where)}' if where else ''
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT r.id, r.scenario_id, r.session_id, r.status, r.step_no,'
            '       r.error, r.trace, r.extract, r.duration_ms, r.total_ms,'
            '       r.created_at, r.film, r.film_frames, s.name AS scenario_name'
            '  FROM browser_scenario_run r'
            '  JOIN browser_scenario s ON s.id = r.scenario_id'
            f'{clause} ORDER BY r.id DESC LIMIT %s, %s',
            (*args, int(offset), int(limit)))
        rows = await db.fetchall() or []

    return [dict(row) for row in rows]


# Где лежат плёнки прогонов.
#
# ⚠⚠ В каталоге данных проекта, а не рядом с кодом: это данные, их не кладут в
# репозиторий и чистят отдельно. Имя файла — номер прогона: он уникален по
# построению, и двух плёнок с одним именем не бывает даже при одновременных
# прогонах.
BROWSER_SCENARIO_FILM_DIR = 'data/browser/film'

# Какой формы бывает имя плёнки. ⚠ Имя задаём мы (номер прогона), и всё, что
# на него не похоже, — отказ: по строке из базы потом отдают файл, и чужая
# форма здесь стоила бы чужого файла.
BROWSER_SCENARIO_FILM_OK = re.compile(r'^\d+\.gif$')


async def browser_scenario_film_path(run_id: int) -> str:
    """Файл плёнки прогона; её нет — пустая строка.

    ⚠ Путь собирается ЗДЕСЬ, а не хранится в базе целиком: в базе лежит имя
    (`<номер>.gif`), и перенос каталога не требует переписывать журнал.
    """
    async with mysql_get_db_async() as db:
        await db.execute('SELECT `film` FROM `browser_scenario_run`'
                         ' WHERE `id` = %s', (int(run_id),))
        row = await db.fetchone()

    name = str((row or {}).get('film') or '')
    if not BROWSER_SCENARIO_FILM_OK.match(name):
        return ''

    whole = pathlib.Path(project_root()) / BROWSER_SCENARIO_FILM_DIR / name

    return str(whole) if whole.is_file() else ''


async def browser_scenario_film_save(run_id: int, frames: list) -> dict:
    """Склеить кадры в плёнку прогона и записать её к нему.

    Args:
        run_id: прогон, которому плёнка принадлежит. Он же имя файла: номер
            уникален по построению, и двух плёнок с одним именем не бывает.
        frames: кадры строками `data:`, как их отдаёт контейнер.

    Returns:
        `{'film', 'frames'}`; плёнки не вышло — нули.

    ⚠⚠ Зовёт её ТОТ, КТО ВЕДЁТ СЦЕНАРИЙ ЦЕЛИКОМ. Сам прогон этого сделать не
    может: его гоняют и пошагово — отрезками по одному шагу, чтобы
    рассказывать о каждом, — и для хранилища каждый такой вызов отдельный
    прогон. Собранная там плёнка состояла бы из одного кадра, а их было бы
    семьдесят одна на карточку. Поймано живьём.

    ⚠ Отказ не роняет работу и не правит статус прогона: плёнка —
    свидетельство о нём, а не он сам.
    """
    return await _film_save(int(run_id), frames or [])


async def browser_scenario_total(run_id: int, total_ms: int) -> None:
    """Отметить, сколько шла РАБОТА ЦЕЛИКОМ, частью которой был этот прогон.

    Args:
        run_id: прогон, на котором работа кончилась, — к нему же вешают плёнку.
        total_ms: длительность всей работы в миллисекундах.

    ⚠⚠ Зовёт тот, кто ведёт сценарий ОТРЕЗКАМИ. Длинный сценарий гоняют по
    шагу за поход, чтобы рассказывать о каждом, и для хранилища каждый поход —
    отдельный прогон. Своё `duration_ms` у них честное, но крошечное: замер на
    живой карточке дал 357 мс при работе в минуту. Связи «эти семьдесят
    записей — одна работа» в базе нет, она есть только у ведущего, — поэтому
    итог приносит он.

    ⚠ `duration_ms` при этом НЕ трогается: по нему ищут медленный шаг, и
    переписанный суммой он показал бы виновником невиновного.

    ⚠ Прогону, пущенному целиком, звать это незачем: работа и есть он сам.
    Показ берёт `total_ms`, а при нуле — `duration_ms`.
    """
    if not run_id or int(total_ms) <= 0:
        return

    async with mysql_get_db_async() as db:
        await db.execute(
            'UPDATE `browser_scenario_run` SET `total_ms` = %s WHERE `id` = %s',
            (int(total_ms), int(run_id)))


async def browser_scenario_execute(scenario: dict, session_id: str = '', variables: dict | None = None,
                                   secrets: dict | None = None, keep_session: bool = False,
                                   proxy: str = '', user_agent: str = '',
                                   extra_headers: dict | None = None,
                                   film: bool = False) -> dict:
    """Один прогон сохранённого сценария: отдать контейнеру, записать журнал, поправить метку.

    Возвращает ответ контейнера как есть: код запроса тут ни при чём, упавший на
    третьем шаге сценарий — это `status: "error"` с трассой, а не ошибка HTTP.

    ⚠ `proxy` действует только на **временную** вкладку (`session_id` пуст): у
    готовой сессии выход задан при открытии, и менять его на ходу Playwright не
    умеет. Строкой, а не номером: у контейнера базы нет, разворачивает реестр тот,
    кто его ведёт.

    Секреты — отдельной сумкой не формальности: контейнер замазывает их в трассе и
    в текстах ошибок, а `variables` попадают в журнал как есть. Пароль, положенный
    не в ту сумку, уехал бы в журнал открытым текстом — то самое, от чего его и шифруют.

    ⚠ `scenario` — строка из `browser_scenario_get()` или `browser_scenario_find()`:
    с разобранными шагами и значениями, как их отдаёт хранилище.

    ⚠⚠ `film` — сложить ли кадры прогона плёнкой к нему же. Умолчание
    `False`, и это не осторожность ради осторожности: сценарий заказа гоняют
    **отрезками** по одному этапу, и для хранилища каждый отрезок — отдельный
    прогон. Плёнка вышла бы из одного кадра, а записей — семьдесят одна на
    карточку. Тот, кто ведёт сценарий целиком, копит кадры сам и кладёт их
    одной плёнкой (`browser_scenario_film_save`).

    ⚠ Разовому прогону (страница «Сценарии», сбор данных) ставить `True`
    стоит: история без плёнки отвечает «сценарий отработал», но не «что он при
    этом видел», — а смотрят её ровно за вторым.
    """
    row = scenario
    scenario_model = BrowserScenario(name=row['name'], goal=row.get('goal', ''),
                                     steps=_json_load(row.get('steps'), []))
    site_plain, site_secret = ({}, {})
    if row.get('site_id'):
        site_plain, site_secret = await browser_site_variable_values(int(row['site_id']))

    result = await browser_upstream_request(
        'POST', '/browser/scenario/run',
        {
            'scenario': scenario_model.model_dump(),
            'session_id': session_id,
            # Три слоя, от общего к частному: переменные сайта, свои значения
            # сценария, переданные на запуск. Побеждает более частое.
            'variables': {**site_plain, **_variables_default(row), **_filled(variables)},
            'secrets': {**site_secret, **_filled(secrets)},
            'keep_session': keep_session,
            'proxy': proxy,
            # Кем представляется вкладка. ⚠ Берутся **вместе**: строка без
            # согласованных заголовков хуже честного умолчания (`useragent_`).
            'user_agent': user_agent,
            'extra_headers': extra_headers or None,
        }, timeout=BROWSER_UPSTREAM_SCENARIO_TIMEOUT)

    # ⚠⚠ Номер прогона уходит в ответ, и это не формальность: по нему
    # зовущий кладёт плёнку (`browser_scenario_film_save`). Сам прогон её не
    # собирает — его гоняют и пошагово, отрезками по одному шагу, и плёнка
    # вышла бы из одного кадра, а записей — семьдесят одна на карточку.
    result['run_id'] = await _run_save(int(row['id']), result)

    # Сломанный сценарий помечается сразу: по метке видно, что чинить, без чтения журнала.
    status = 'broken' if result.get('status') == 'error' else 'active'
    if row.get('status') != status:
        async with mysql_get_db_async() as db:
            await db.execute('UPDATE browser_scenario SET status = %s WHERE id = %s',
                             (status, int(row['id'])))
        browser_scenario_forget(row['name'])

    # ⚠ Плёнка кладётся ПОСЛЕ записи прогона: её имя — его номер, и без
    # записанного прогона класть её некуда. Отказ здесь молчит: плёнка —
    # свидетельство о работе, а не сама работа.
    if film and result.get('frames'):
        await _film_save(int(result['run_id']), result['frames'])

    return result


def _pace_values(low, high) -> tuple:
    """Пределы темпа к записи: `(низ, верх)`, оба в границах колонки.

    ⚠ `None` у обоих — умолчания слоя: так заводится новый сценарий, и ноль
    здесь был бы хуже — он значит «без пауз», то есть нечеловеческий ритм по
    умолчанию.

    ⚠ Перевёрнутые границы выпрямляются: опечатка в форме не повод не дать
    сохранить.

    ⚠ Потолок — 60 000 мс: столько же, сколько у колонки `SMALLINT UNSIGNED`
    (65 535). Больше минуты между шагами — это уже не ритм, а остановка.
    """
    if low is None and high is None:
        return BROWSER_SCENARIO_PACE_MIN_MS, BROWSER_SCENARIO_PACE_MAX_MS

    low = max(0, min(60000, int(low or 0)))
    high = max(0, min(60000, int(high or 0)))

    return (high, low) if low > high else (low, high)


def _scenario_check(name: str, goal: str, steps: list) -> BrowserScenario:
    """Проверяет шаги схемой контейнера: ошибку лучше увидеть при сохранении."""
    try:
        return BrowserScenario(name=name, goal=goal, steps=steps)
    except Exception as e:
        raise ValueError(f'сценарий не проходит проверку: {e}')


def _scenario_loaded(row: dict | None) -> dict:
    """Строка базы → сценарий с разобранными JSON-столбцами и списком подстановок."""
    if not row:
        raise KeyError('сценарий не найден')
    row['id'] = int(row['id'])
    row['steps'] = _json_load(row.get('steps'), [])
    row['variables'] = _json_load(row.get('variables'), {})
    try:
        row['variables_used'] = browser_scenario_variables_used(BrowserScenario(
            name=row['name'], goal=row.get('goal', ''), steps=row['steps']))
    except Exception:
        row['variables_used'] = []

    return row


def _variables_default(row: dict) -> dict:
    """Значения по умолчанию из базы, приведённые к строкам."""
    values = _json_load(row.get('variables'), {})

    return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}


def _filled(values: dict | None) -> dict:
    """Переданные значения без пустых: пустое не должно стирать значение сайта.

    Пустая строка не бывает осмысленным паролем: приходит она от незаполненного
    поля, а не от намерения. Настоящее отсутствие переменной ловится проверкой
    контейнера (`browser_scenario_variables_missing`), а не здесь.
    """
    return {name: value for name, value in (values or {}).items() if str(value or '') != ''}


def _json_load(value, default):
    """JSON-столбец приезжает то структурой, то строкой — зависит от драйвера."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default

    return value if value is not None else default


async def _film_save(run_id: int, frames: list) -> dict:
    """Склеить кадры прогона в GIF и записать его имя к прогону.

    Args:
        run_id: номер прогона — он же имя файла.
        frames: кадры строками `data:`, как их отдал контейнер.

    Returns:
        `{'film', 'frames'}`; плёнки не вышло — нули.

    ⚠⚠ Склеивает ЭТА сторона, а не контейнер. У контейнера нет ни базы, ни
    каталога данных проекта: он отдаёт кадры и забывает о них. Здесь же есть
    и номер прогона, под которым плёнка ляжет.

    ⚠ Отказ не роняет прогон и не правит его статус: плёнка — свидетельство о
    работе, а не сама работа. Сценарий, прошедший успешно, не должен
    становиться неудачным из-за несобравшегося GIF.
    """
    shots = [_film_bytes(one) for one in (frames or [])]
    shots = [one for one in shots if one]
    if len(shots) < 2:
        return {'film': '', 'frames': 0}

    name = f'{int(run_id)}.gif'
    whole = pathlib.Path(project_root()) / BROWSER_SCENARIO_FILM_DIR / name
    try:
        got = image_film_gif(shots, str(whole))
    except Exception as fault:  # noqa: BLE001 — плёнка не стоит прогона
        logger_info(f'[browser] плёнка прогона {run_id} не собралась: {fault}')

        return {'film': '', 'frames': 0}

    if got.get('error'):
        return {'film': '', 'frames': 0}

    async with mysql_get_db_async() as db:
        await db.execute(
            'UPDATE `browser_scenario_run` SET `film` = %s, `film_frames` = %s'
            '  WHERE `id` = %s', (name, int(got['frames']), int(run_id)))

    return {'film': name, 'frames': int(got['frames'])}


def _film_bytes(uri: str) -> bytes:
    """Кадр из строки `data:` байтами; не разобралось — пусто.

    ⚠ Строка приходит и как `data:image/jpeg;base64,…`, и голым base64 —
    зависит от версии контейнера. Берём хвост после запятой в обоих случаях.
    """
    text = str(uri or '')
    if not text:
        return b''

    try:
        return base64.b64decode(text.split(',', 1)[-1])
    except Exception:  # noqa: BLE001 — кадр не стоит прогона
        return b''


async def _run_save(scenario_id: int, result: dict) -> int:
    """Кладёт трассу в журнал одной строкой; возвращает номер записи.

    Секретов в ней нет — контейнер маскирует их до ответа.

    ⚠ Номер нужен плёнке: она ложится файлом с этим именем (`_film_save`), и
    придумывать ей второй ключ значило бы заводить способ разойтись с
    журналом.
    """
    async with mysql_get_db_async() as db:
        await db.execute(
            'INSERT INTO browser_scenario_run '
            '(scenario_id, session_id, status, step_no, error, trace, extract, duration_ms) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
            (
                int(scenario_id),
                str(result.get('session_id', ''))[:32],
                'error' if result.get('status') == 'error' else 'ok',
                int(result.get('step_no', 0) or 0),
                str(result.get('error', '')),
                json.dumps(result.get('steps', []), ensure_ascii=False),
                json.dumps(result.get('extract', {}), ensure_ascii=False),
                int(result.get('duration_ms', 0) or 0),
            )
        )
        await db.execute('SELECT LAST_INSERT_ID() AS id')

        return int((await db.fetchone())['id'])

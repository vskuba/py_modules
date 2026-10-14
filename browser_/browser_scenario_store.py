"""Хранилище сценариев и их прогон со стороны проекта: найти, сохранить, выполнить.

Контейнер (`browser_scenario_run`) сценариев не хранит и в журнал не пишет — он
отвечает трассой тому, кто попросил. Хранением, журналом и меткой состояния
занимается этот модуль: второй потребитель базы дал бы контейнеру связность там,
где он её специально не имеет — упавшая база не должна ронять живые сессии.

Здесь же — слои значений при прогоне: переменные сайта снизу, значения самого
сценария поверх, переданное на запуск — всего. Побеждает более частное; пустые
переданные значения не стирают сохранённых.
"""

import json
import time

from browser_.browser_scenario import BrowserScenario, browser_scenario_variables_used
from browser_.browser_upstream import (browser_upstream_request,
                                       BROWSER_UPSTREAM_SCENARIO_TIMEOUT)
from browser_.browser_site_variable import browser_site_variable_values
from mysql_.mysql_ import mysql_get_db_async

# Сколько жить найденному сценариванию в памяти процесса: исполнитель зовёт его на
# каждую карточку, а правка сценария — редкость. После сохранения кэш всё равно
# выбрасывается сразу, TTL — лишь страховка для правки, сделанной мимо модуля.
BROWSER_SCENARIO_CACHE_TTL_SEC = 600.0

# Найденные сценарии: имя → (строка, момент нахождения). Имя уникально в таблице,
# потому и ключа хватает одного.
_CACHE: dict[str, tuple] = {}


async def browser_scenario_save(name: str, steps: list, site_id: int = 0, goal: str = '',
                                start_url: str = '', variables: dict | None = None,
                                status: str = 'draft', scenario_id: int = 0) -> int:
    """Завести или переписать сценарий; возвращает его номер.

    Шаги проходят проверку схемой контейнера здесь, при сохранении: ошибку лучше
    увидеть сейчас, а не в первом прогоне. Секретов в `variables` нет — значения
    по умолчанию едут в журнал открытыми; секреты живут у сайта
    (`browser_site_variable`).

    Raises:
        ValueError: шаги не прошли схему, имя занято другим сценарием или статусу
            такой не положен.
    """
    scenario = _scenario_check(name, goal, steps)
    async with mysql_get_db_async() as db:
        if scenario_id:
            await db.execute('SELECT id FROM browser_scenario WHERE id = %s', (int(scenario_id),))
            if not await db.fetchone():
                raise KeyError('сценарий не найден')
            await db.execute(
                'UPDATE browser_scenario'
                '   SET site_id = %s, name = %s, goal = %s, start_url = %s,'
                '       steps = %s, variables = %s, status = %s WHERE id = %s',
                (site_id or None, name, goal, start_url,
                 json.dumps([s.model_dump() for s in scenario.steps], ensure_ascii=False),
                 json.dumps(variables or {}, ensure_ascii=False), status, int(scenario_id)))

            browser_scenario_forget(name)

            return int(scenario_id)

        await db.execute(
            'SELECT 1 FROM browser_scenario WHERE name = %s LIMIT 1', (name,))
        if await db.fetchone():
            raise ValueError(f'сценарий с именем «{name}» уже есть')
        await db.execute(
            'INSERT INTO browser_scenario (site_id, name, goal, start_url, steps, variables, status) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s)',
            (site_id or None, name, goal, start_url,
             json.dumps([s.model_dump() for s in scenario.steps], ensure_ascii=False),
             json.dumps(variables or {}, ensure_ascii=False), status))
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
            '       created_at, updated_at '
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


async def browser_scenario_runs(scenario_id: int, limit: int = 50, offset: int = 0) -> list:
    """Журнал прогонов сценария, свежие сверху: статус, шаг, ошибка, трасса, данные."""
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT id, scenario_id, session_id, status, step_no, error, trace, extract,'
            '       duration_ms, created_at '
            '  FROM browser_scenario_run WHERE scenario_id = %s ORDER BY id DESC LIMIT %s, %s',
            (int(scenario_id), int(offset), int(limit)))
        rows = await db.fetchall() or []

    return [dict(row) for row in rows]


async def browser_scenario_execute(scenario: dict, session_id: str = '', variables: dict | None = None,
                                   secrets: dict | None = None, keep_session: bool = False,
                                   proxy: str = '') -> dict:
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
        }, timeout=BROWSER_UPSTREAM_SCENARIO_TIMEOUT)

    await _run_save(int(row['id']), result)

    # Сломанный сценарий помечается сразу: по метке видно, что чинить, без чтения журнала.
    status = 'broken' if result.get('status') == 'error' else 'active'
    if row.get('status') != status:
        async with mysql_get_db_async() as db:
            await db.execute('UPDATE browser_scenario SET status = %s WHERE id = %s',
                             (status, int(row['id'])))
        browser_scenario_forget(row['name'])

    return result


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


async def _run_save(scenario_id: int, result: dict) -> None:
    """Кладёт трассу в журнал одной строкой. Секретов в ней нет — контейнер маскирует их до ответа."""
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

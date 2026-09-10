"""Прогон сценария: шаги выполняются здесь, в контейнере, где живут страницы.

Контейнер сценариев не хранит и в журнал не пишет — он отвечает трассой прогона
тому, кто попросил. Хранением и журналом занимается вызывающий, у которого уже
есть база: второй потребитель базы дал бы контейнеру связность там, где он её
специально не имеет — упавшая база не должна ронять живые сессии.

Трасса возвращается **всегда**, и на ошибке тоже: без неё ответ «не сработало»
бесполезен, а с ней видно, на каком шаге и с чем встали — из этого агент
самопочинки и напишет задачу на переписывание сценария.
"""

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from logging_.logging_ import logger_info

from browser_.browser_api import browser_api_auth
from browser_.browser_console import browser_console_stop
from browser_.browser_pool import (
    browser_pool_session_close,
    browser_pool_session_get,
    browser_pool_session_open,
)
from browser_.browser_scenario import (
    BrowserScenario,
    BrowserScenarioStep,
    browser_scenario_step_target,
    browser_scenario_variables_missing,
)
from browser_.browser_scenario_mask import (browser_scenario_mask,
                                            browser_scenario_mask_data)
from browser_.browser_scenario_net import (browser_scenario_net_new,
                                           browser_scenario_net_stop)
from browser_.browser_scenario_step import browser_scenario_step_apply

router = APIRouter()

# Текст ошибки шага в трассе. Сообщения Playwright многострочные и с «call log»
# на полсотни строк — в журнале от него пользы нет, причина всегда в первых строках.
BROWSER_SCENARIO_RUN_ERROR_LIMIT = 500


class BrowserScenarioRunRequest(BaseModel):
    """Что нужно для прогона: сам сценарий, где его крутить и с какими значениями."""

    scenario: BrowserScenario
    session_id: str = Field('', description='Пусто — завести временную сессию под прогон')
    variables: dict[str, str] = Field(default_factory=dict, description='Значения; попадают в журнал')
    secrets: dict[str, str] = Field(default_factory=dict, description='Значения; в журнал не попадают')
    keep_session: bool = Field(False, description='Не гасить временную сессию после прогона')


@router.post('/browser/scenario/run', dependencies=browser_api_auth)
async def browser_scenario_run_post(data: BrowserScenarioRunRequest):
    """Прогоняет сценарий и отвечает трассой.

    Код ответа — про сам запрос, а не про результат сценария: не тот `session_id`
    это 404, нехватка переменной — 422, а упавший на третьем шаге сценарий — 200
    со `status: error`. Иначе журнал прогонов пришлось бы собирать из кодов HTTP.
    """
    try:
        return await browser_scenario_run(
            data.scenario,
            session_id=data.session_id,
            variables=data.variables,
            secrets=data.secrets,
            keep_session=data.keep_session,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


async def browser_scenario_run(scenario: BrowserScenario, session_id: str = '',
                               variables: dict | None = None, secrets: dict | None = None,
                               keep_session: bool = False) -> dict:
    """Выполняет шаги по порядку и возвращает трассу прогона.

    Первый упавший шаг останавливает прогон: следующие шаги рассчитывают на то,
    что предыдущий сработал, и продолжать значило бы получить лавину ошибок вместо
    одной настоящей. Исключение — шаги с `optional`: баннер согласия может и не
    появиться, и его отсутствие не сбой.
    """
    variables = variables or {}
    secrets = secrets or {}
    values = {**variables, **secrets}

    missing = browser_scenario_variables_missing(scenario, values)
    if missing:
        raise ValueError(f'не переданы переменные: {", ".join(missing)}')

    entry, own_session = await _session_take(session_id, scenario.name)
    page = entry['page']
    # Наблюдатели — состояние **на прогон**, а не на страницу.
    #
    # ⚠ Сессию переиспользуют (`keep_session`, живое окно человека), и слушатель,
    # оставленный на странице, дожил бы до следующего прогона: тот получил бы чужие
    # записи, а список рос бы бесконечно. Поэтому состояние здесь, а снимаются
    # слушатели в `finally` — вместе с прогоном, чем бы тот ни кончился.
    state = {'net': browser_scenario_net_new(), 'console': None}
    started = time.monotonic()
    steps: list[dict] = []
    extract: dict = {}   # строка у `extract`, список записей у построчного сбора
    status = 'ok'
    error = ''
    failed_no = 0
    final_url = ''

    try:
        for index, step in enumerate(scenario.steps, start=1):
            record = await _step_run(page, step, index, values, secrets, state)
            steps.append(record)

            if record['name'] and record['ok']:
                # `data` есть только у сбора записей — там записи, а в `value`
                # лежит их счёт для трассы. У прочих шагов данных нет, и в ход идёт
                # само значение.
                extract[record['name']] = (record['data'] if record['data'] is not None
                                           else record['value'])

            if not record['ok']:
                status = 'error'
                error = record['error']
                failed_no = index
                break

        # Адрес снимается до закрытия сессии: у закрытой страницы его уже не спросить,
        # а «где мы оказались» — половина ответа на вопрос, сработал ли сценарий.
        final_url = page.url
        entry['used_at'] = time.time()
    finally:
        browser_scenario_net_stop(page, state['net'])
        browser_console_stop(state['console'])
        if own_session and not keep_session:
            await browser_pool_session_close(entry['session_id'])

    logger_info(f'[browser] сценарий «{scenario.name}»: {status}, шагов {len(steps)} из {len(scenario.steps)}')

    return {
        'status': status,
        'session_id': entry['session_id'],
        'session_kept': not own_session or keep_session,
        'url': final_url,
        'step_no': failed_no,
        'error': error,
        'steps': steps,
        'extract': extract,
        'duration_ms': round((time.monotonic() - started) * 1000),
    }


async def _session_take(session_id: str, name: str) -> tuple[dict, bool]:
    """Сессия прогона и признак «завели её мы».

    Свою сессию гасим после прогона, чужую — никогда: сценарий часто крутят на
    странице, которую человек в этот момент смотрит в живом окне.
    """
    if session_id:
        entry = browser_pool_session_get(session_id)
        if entry is None:
            raise KeyError(session_id)
        if entry['page'].is_closed():
            raise RuntimeError('страница сессии закрыта')
        return entry, False

    session = await browser_pool_session_open(name=f'сценарий: {name}'[:100])
    return browser_pool_session_get(session['session_id']), True


async def _step_run(page, step: BrowserScenarioStep, index: int, values: dict,
                    secrets: dict, state: dict | None = None) -> dict:
    """Один шаг с замером и разбором ошибки. Исключения наружу не выпускает:
    упавший шаг — это строка трассы, а не сбой прогона."""
    started = time.monotonic()
    value = ''
    ok = True
    error = ''

    try:
        value = await browser_scenario_step_apply(page, step, values, state)
    except Exception as e:
        ok = bool(step.optional)
        error = browser_scenario_mask(str(e), secrets).strip()[:BROWSER_SCENARIO_RUN_ERROR_LIMIT]

    # Трасса и собранные данные — врозь. Оба поля уезжают в базу, но в разные
    # колонки (`trace` и `extract`), и список из пятисот записей, положенный в оба,
    # задвоил бы объём строки журнала. В трассе остаётся счёт записей: по нему видно,
    # сработал шаг или вернул пусто, а сами записи лежат рядом, в `extract`.
    data = value if isinstance(value, list) else None
    shown = f'записей: {len(value)}' if isinstance(value, list) else value

    return {
        'index': index,
        'action': step.action,
        # Цель — до подстановки: в журнале должно остаться `{{password}}`, а не пароль.
        'target': browser_scenario_step_target(step),
        'name': step.name,
        'ok': ok,
        'optional': step.optional,
        'error': error,
        'value': browser_scenario_mask(shown, secrets),
        'data': browser_scenario_mask_data(data, secrets),
        'ms': round((time.monotonic() - started) * 1000),
    }

"""Прогон сценария: шаги выполняются здесь, в контейнере, где живут страницы.

Контейнер сценариев не хранит и в журнал не пишет — он отвечает трассой прогона
тому, кто попросил. Хранением и журналом занимается вызывающий, у которого уже
есть база: второй потребитель базы дал бы контейнеру связность там, где он её
специально не имеет — упавшая база не должна ронять живые сессии.

Трасса возвращается **всегда**, и на ошибке тоже: без неё ответ «не сработало»
бесполезен, а с ней видно, на каком шаге и с чем встали — из этого агент
самопочинки и напишет задачу на переписывание сценария.
"""

import base64
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from image_.image_film import image_film_same
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
    # Выход наружу для временной вкладки — строкой, а не номером: у контейнера
    # базы нет, реестр разворачивает тот, кто его ведёт.
    user_agent: str = Field('', max_length=400,
                            description='Кем представляется браузер')
    extra_headers: dict | None = Field(
        None, description='Заголовки, согласованные с user_agent')
    proxy: str = Field('', max_length=500,
                       description='Выход временной вкладки: socks5://user:pass@host:1080')


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
            proxy=data.proxy,
            user_agent=data.user_agent,
            extra_headers=data.extra_headers,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


async def browser_scenario_run(scenario: BrowserScenario, session_id: str = '',
                               variables: dict | None = None, secrets: dict | None = None,
                               keep_session: bool = False, proxy: str = '',
                               user_agent: str = '',
                               extra_headers: dict | None = None,
                               film: bool = True) -> dict:
    """Выполняет шаги по порядку и возвращает трассу прогона.

    `user_agent` и `extra_headers` — кем представляется временная вкладка.
    ⚠ У готовой сессии (`session_id` задан) они уже заданы при открытии и здесь
    не действуют: заголовки контекста Playwright по ходу не меняет.

    `proxy` — выход наружу для **временной** вкладки, которую прогон заводит сам
    (`session_id` пуст). Готовая сессия свой выход уже имеет, и подменять его
    нельзя: он задан при её открытии.

    ⚠ **Без этого сценарий входа ходил бы с общего адреса.** Прогон с пустым
    `session_id` — обычное дело (отправка, звёздочка, карточка собеседника), и
    внутри он логинится под учёткой. Сайты, считающие вход по IP, складывали бы
    все такие входы на один адрес сервера.

    Первый упавший шаг останавливает прогон: следующие шаги рассчитывают на то,
    что предыдущий сработал, и продолжать значило бы получить лавину ошибок вместо
    одной настоящей. Исключение — шаги с `optional`: баннер согласия может и не
    появиться, и его отсутствие не сбой.

    `film` — снимать плёнку прогона: кадр после каждого шага, что-то
    изменившего на экране. Кадры приходят в `frames` строками `data:` и
    складываются в GIF (`image_film_gif`) тем, кому он нужен.

    ⚠⚠ Снимает САМ прогон, а не вызывающий: страница у него под рукой, и кадр
    стоит один вызов. Снаружи тот же кадр стоил бы похода по HTTP на каждый
    шаг — сорок походов на сценарий.

    ⚠ Одинаковые подряд не копятся: половина шагов ничего на экране не меняет,
    и плёнка без этой проверки стала бы лентой из сорока похожих кадров.
    """
    variables = variables or {}
    secrets = secrets or {}
    values = {**variables, **secrets}

    missing = browser_scenario_variables_missing(scenario, values)
    if missing:
        raise ValueError(f'не переданы переменные: {", ".join(missing)}')

    entry, own_session = await _session_take(session_id, scenario.name, proxy,
                                             user_agent, extra_headers)
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
    reel: list = []
    # ⚠ Номера шагов, которым отвечают кадры: кадр снимается не на каждом
    # шаге, и порядок кадров шагам не равен (см. `_film_take`).
    marks: list = []

    try:
        for index, step in enumerate(scenario.steps, start=1):
            record = await _step_run(page, step, index, values, secrets, state)
            steps.append(record)

            # ⚠ Кадр снимается и у упавшего шага: именно он показывает, на чём
            # сценарий сломался, — а это и есть главный вопрос к плёнке.
            if film:
                await _film_take(page, reel, marks, index)

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
        # ⚠ Кадры строками `data:`, как и прочие картинки слоя: ответ едет по
        # HTTP, и байты в JSON не положишь (`browser_capture.py`).
        'frames': [_film_uri(shot) for shot in reel],
        # ⚠ Номер шага на каждый кадр, тем же порядком: по ним плёнку
        # подписывают. Пустой список у прогона без кадров — не отсутствие
        # данных, а отсутствие кадров.
        'frame_steps': list(marks),
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


# Снимать ли плёнку прогона: кадр после каждого шага, что-то изменившего.
#
# ⚠⚠ Снимает САМ контейнер, а не вызывающий. Кадр здесь стоит один запрос к
# странице, которая уже под рукой; у вызывающего он стоил бы похода по HTTP на
# каждый шаг — сорок походов на сценарий.
#
# ⚠ Одинаковые подряд не копятся: половина шагов ничего на экране не меняет
# (паузы, чтения, вычисления), и без проверки плёнка превращается в ленту из
# сорока почти одинаковых кадров.
BROWSER_SCENARIO_FILM_MAX = 24


async def _film_take(page, film: list, marks: list, step_no: int,
                     quality: int = 55) -> None:
    """Снять кадр шага в плёнку, если он что-то изменил на странице.

    ⚠⚠ Страница ЦЕЛИКОМ (`full_page`). Вьюпорт показывает только верх: форма
    видна, а кнопки, итоги и то, что ниже края, — нет. По плёнке разбирают
    работу, и обрезанный кадр отвечает на половину вопроса.

    ⚠ `captureBeyondViewport` у Playwright снимает длинную страницу БЕЗ
    прокрутки: сценарий на ней не спотыкается, и кадр после каждого шага
    безопасен.

    ⚠⚠ Отказ снимка не роняет прогон и даже не попадает в трассу: плёнка —
    свидетельство о работе, а не сама работа. Страница могла уйти на другой
    адрес, закрыться, зависнуть — прогон разберётся с этим сам, своими шагами.

    ⚠ Рядом с кадром кладётся НОМЕР ШАГА, после которого он снят. Кадр
    снимается только тогда, когда шаг что-то изменил, и порядковый номер
    кадра шагу не равен: третий кадр — обычно седьмой шаг. Без этой пары
    подписать плёнку нечем, а подпись и есть то, ради чего её смотрят.
    """
    if len(film) >= BROWSER_SCENARIO_FILM_MAX:
        return

    try:
        shot = await page.screenshot(type='jpeg', quality=quality,
                                     full_page=True, timeout=8000)
    except Exception:  # noqa: BLE001 — кадр не стоит прогона
        return

    if shot and not image_film_same(film[-1] if film else b'', shot):
        film.append(shot)
        marks.append(int(step_no))


def _film_uri(shot: bytes) -> str:
    """Кадр плёнки строкой `data:` — в том же виде, что и прочие картинки слоя."""
    return 'data:image/jpeg;base64,' + base64.b64encode(shot).decode()


async def _session_take(session_id: str, name: str, proxy: str = '',
                        user_agent: str = '',
                        extra_headers: dict | None = None) -> tuple[dict, bool]:
    """Сессия прогона и признак «завели её мы».

    Свою сессию гасим после прогона, чужую — никогда: сценарий часто крутят на
    странице, которую человек в этот момент смотрит в живом окне.

    ⚠ `proxy`, `user_agent` и `extra_headers` действуют только на **свою**
    вкладку: у готовой сессии выход и отпечаток заданы при открытии, и менять
    их на ходу Playwright не умеет — это свойства контекста, а не запроса.

    ⚠⚠ Параметры отпечатка тело функции использовало, а сигнатура не
    объявляла: правка была дописана наполовину, и прогон падал
    `TypeError: _session_take() takes from 2 to 3 positional arguments but 5
    were given`. Наружу это выходило пустым ответом контейнера и строкой
    «Expecting value: line 1 column 1» в карточке — по ней причину не узнать.
    """
    if session_id:
        entry = browser_pool_session_get(session_id)
        if entry is None:
            raise KeyError(session_id)
        if entry['page'].is_closed():
            raise RuntimeError('страница сессии закрыта')
        return entry, False

    session = await browser_pool_session_open(
        name=f'сценарий: {name}'[:100], proxy=proxy,
        user_agent=user_agent, extra_headers=extra_headers)
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

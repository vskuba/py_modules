"""Пул браузера: один Chromium на процесс, сессия — отдельный BrowserContext.

Почему контекст, а не отдельный браузер на сессию: контекст изолирует cookie,
localStorage и кэш полностью — сессии друг друга не видят, — но стоит единицы
мегабайт, тогда как процесс Chromium стоит ~150 МБ. Память съедают открытые
страницы, и ограничивать надо именно их число.

Состояние модульное, а не в классе: Chromium один на процесс, и контейнер этим
пулом и является. Реестр живёт в памяти — контейнер упал, значит и сессий больше
нет, и строка в БД говорила бы неправду до первой уборки.

⚠ **Запись сессии — общая тетрадь.** Кроме страницы и контекста в неё кладут своё
состояние соседи: запись действий (`record`), инспектор (`inspect_*`), наблюдатель
DOM (`watch`), журнал консоли (`console`). Так это состояние гаснет вместе с
сессией само и не заводит второй реестр, который пришлось бы синхронизировать.
"""

import asyncio
import json
import time
import uuid
from urllib.parse import unquote

from playwright.async_api import async_playwright, Browser, Playwright

from config.config import config_get
from http_.http_proxy import http_proxy_hide
from logging_.logging_ import logger_info

# Предел на число живых страниц. Ограничение не Chromium-а — он выдержит больше, —
# а предела памяти контейнера. Замер на 10 сессиях: сам браузер ~200 МБ, пустая
# сессия +14 МБ, статья энциклопедии +83 МБ. То есть 4 ГБ — это ~45 страниц такого
# веса, а страница чата (SPA живёт дольше и растёт) стоит вдвое дороже. 25 —
# половина расчётного максимума: запас на разрастание долгоживущих страниц.
# Меняется переменной, замерять — через `GET /browser/session`, там память.
BROWSER_POOL_SESSION_LIMIT = int(config_get('BROWSER_SESSION_LIMIT', '25'))

# Сколько сессия может жить без прикосновений, после чего пул закрывает её сам.
# 0 — не закрывать.
#
# ⚠⚠ Правило против вкладок, переживших прогоны. Сценарий с `keep_session`
# оставляет вкладку открытой после себя — и пока она открыта, сайт считает
# анкету «в сети». Живьём: вкладка одного прогона держала анкету в сети шесть
# часов после работы, когда сам прогон был давным-давно успешен и ни при чём.
#
# ⚠ Нужной сессии правило не грозит: любое прикосновение — переход, шаг
# прогона, живое окно, любой запрос к сессии через `browser_pool_session_get` —
# обновляет `used_at`. Закрывается только то, к чему давно никто не обращался;
# сессии с живым слушателем (запись, наблюдение) — вне удара, см. `_pool_busy`.
BROWSER_POOL_IDLE_SEC = int(config_get('BROWSER_SESSION_IDLE_MIN', '60')) * 60

# С какой частотой смотреть холостые. Минута — не часы и не секунды: просрочка
# срока не должна расти до суток, но и крутиться чаще минуты ради числа,
# округлённого до минут, нечем.
BROWSER_POOL_SWEEP_SEC = 60

# Порог жертвы при переполнении: сессию, тронутую меньше минуты назад, не
# вытесняем — с ней работают прямо сейчас.
BROWSER_POOL_EVICT_GRACE_SEC = 60

BROWSER_POOL_VIEWPORT_WIDTH = 1280
BROWSER_POOL_VIEWPORT_HEIGHT = 800
BROWSER_POOL_GOTO_TIMEOUT_MS = 30000

# `--disable-dev-shm-usage` здесь сознательно нет: он уводит Chromium с /dev/shm
# на диск и замедляет отрисовку. Ту же болезнь (64 МБ shm по умолчанию, на которых
# тяжёлая страница роняет вкладку) лечит `shm_size` контейнера — правильно.
BROWSER_POOL_LAUNCH_ARGS = [
    '--no-sandbox',  # контейнер изолирован сам, а user namespaces внутри него недоступны
    '--disable-gpu',
    # ⚠⚠ Без этого Chromium САМ объявляет себя роботом: `navigator.webdriver`
    # равен `true`, и проверять больше нечего — ни отпечаток, ни прокси уже не
    # помогут. Цена известна и описана в `x11_fingerprint_eval`: «тупой отказ
    # капчи без ошибки». Замер на живой установке: тридцать заказов прошли,
    # тридцать первый получил капчу.
    #
    # ⚠ Это флаг ЗАПУСКА, не контекста: он действует на весь браузер, и чтобы
    # применился — контейнер надо перезапустить, а не просто открыть сессию.
    '--disable-blink-features=AutomationControlled',
]

# Заголовки отпечатка, которые НЕЛЬЗЯ вешать на контекст.
#
# ⚠⚠ Они осмысленны только для навигации, а `extra_http_headers` уходит с
# КАЖДЫМ запросом — со стилями, скриптами, картинками, XHR. Страница после
# этого открывается без стилей и без своего JS: разметка на месте, элементы
# находятся, клики проходят — и не делают ничего. Разбор и замер —
# `_pool_headers`.
#
# ⚠ Имена в нижнем регистре: заголовки регистронезависимы, а в снимке они
# приходят то `Sec-Fetch-Dest`, то `sec-fetch-dest`.
BROWSER_POOL_HEADERS_SKIP = frozenset({
    # ⚠⚠ Эти четыре Chromium считает своими: подзапрос с заданным вручную
    # `Sec-Fetch-*` он не отправляет вовсе — `net::ERR_INVALID_ARGUMENT`
    # ещё до выхода в сеть. Именно они и гасят стили со скриптами.
    'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site', 'sec-fetch-user',
    # ⚠ Эти два запрос не ломают, но врут о нём: `Accept: text/html` на
    # стиле и `Upgrade-Insecure-Requests` на XHR не бывают ни у одного
    # живого браузера, то есть работают против подмены, а не на неё.
    'accept',
    'upgrade-insecure-requests',
    # ⚠ `User-Agent` тоже сюда: его задаёт свой довод контекста
    # (`user_agent`), и вторая копия в заголовках спорила бы с ним.
    'user-agent',
})

# Что страница видит о себе до единой своей строки.
#
# ⚠⚠ Ставится ТОЛЬКО вместе с `user_agent` — см. `_pool_mask`. Без него
# подгонять не к чему, а выдуманный `platform` при честном UA создаёт то самое
# противоречие, ради устранения которого всё и затевалось.
#
# ⚠ Три правки, а не двадцать. `window.chrome` и непустой `navigator.plugins`
# есть у любого настоящего Chrome, а `platform` обязан отвечать своему UA —
# «Macintosh» в строке при `Linux x86_64` в свойстве не бывает ни у кого.
# Дальше начинается гонка (canvas, WebGL, шрифты), которую подменой свойств не
# выигрывают: это уровень процесса, а процесс Chromium у всех сессий один.
BROWSER_POOL_MASK_JS = """(() => {
  const plat = %(platform)s;
  if (plat) Object.defineProperty(navigator, 'platform', {get: () => plat});
  if (!window.chrome) window.chrome = {runtime: {}};
  if (!navigator.plugins || !navigator.plugins.length) {
    const made = [1, 2, 3, 4, 5].map(n => ({name: 'Plugin ' + n}));
    Object.defineProperty(navigator, 'plugins', {get: () => made});
  }
})()"""

_playwright: Playwright | None = None
_browser: Browser | None = None
_sessions: dict[str, dict] = {}
_lock = asyncio.Lock()
_sweeper: asyncio.Task | None = None


async def browser_pool_start():
    """Поднимает Chromium и обход холостых. Зовётся один раз из lifespan контейнера."""
    global _playwright, _browser, _sweeper

    if _browser is not None:
        return

    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True, args=BROWSER_POOL_LAUNCH_ARGS)
    idle = (f', холостой ход {BROWSER_POOL_IDLE_SEC // 60} мин' if BROWSER_POOL_IDLE_SEC
            else ', закрытие холостых выключено')
    logger_info(f'[browser] Chromium запущен: {_browser.version}, предел сессий '
                f'{BROWSER_POOL_SESSION_LIMIT}{idle}')

    if BROWSER_POOL_IDLE_SEC:
        _sweeper = asyncio.create_task(_idle_sweep())


async def browser_pool_stop():
    """Гасит обход, все сессии и сам браузер."""
    global _playwright, _browser, _sweeper

    if _sweeper is not None:
        _sweeper.cancel()
        _sweeper = None

    for session_id in list(_sessions):
        await browser_pool_session_close(session_id)

    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None

    logger_info('[browser] Chromium остановлен')


async def browser_pool_session_open(name: str = '', url: str = '', storage_state: dict | None = None,
                                    viewport: dict | None = None, site_id: int = 0,
                                    locale: str = '', timezone_id: str = '',
                                    proxy: str = '', user_agent: str = '',
                                    extra_headers: dict | None = None) -> dict:
    """Заводит сессию: свой контекст, одна страница в нём.

    `storage_state` — сохранённые cookie и localStorage прошлой сессии: с ним
    страница открывается уже авторизованной, без повторного прохода по форме входа.

    `locale` и `timezone_id` — не украшение: язык интерфейса решает, какую вёрстку
    отдаст сайт, а вместе с ней — совпадут ли селекторы сценария и найдётся ли
    текст в `assert_text`. Пусто — как настроен образ.

    `site_id` — метка вызывающего: чем он сам эту сессию помечает (у нас — закладка
    сайта в его базе). Пул её не проверяет и ничего о ней не знает; в базу она не
    уезжает и уезжать не должна — сессия живёт в памяти и умирает вместе с процессом.

    `proxy` — выход наружу для **этой** сессии: `socks5://user:pass@host:1080`.

    `user_agent` и `extra_headers` — кем представляется эта сессия. ⚠⚠ Берут их
    **вместе**: строка `User-Agent` без согласованных `Sec-CH-UA` и
    `Accept-Language` выглядит хуже честного умолчания — сайт смотрит на набор,
    а не на строку (`useragent_.py`). Пусто — как в образе.

    ⚠⚠ Навигационную половину набора пул **отбрасывает** (`Accept`,
    `Sec-Fetch-*`, `Upgrade-Insecure-Requests`, `User-Agent` — он задаётся своим
    доводом): заголовки контекста уходят с КАЖДЫМ запросом страницы, а подзапрос
    с заданным вручную `Sec-Fetch-*` Chromium не отправляет вовсе — страница
    приезжает без стилей и без своего JS. Разбор и замер — `_pool_headers`.
    Потери нет: на самих запросах `Sec-Fetch-*` ставит Chromium, и ставит верно.

    ⚠ **Прокси на контекст, а не на браузер.** Chromium здесь один на процесс, и
    задай мы выход при запуске — через него пошли бы все сессии разом. Сайтам
    знакомств нужно обратное: учётки считаются по внешнему адресу, и у каждой
    анкеты он должен быть свой. Контекст это и даёт, не поднимая второго браузера.

    ⚠ **Пароль сюда приезжает открытым** — иначе через прокси не пройти. В журнал
    он не попадает: строка сессии пишет адрес без него.
    """
    if _browser is None:
        raise RuntimeError('браузер не запущен')

    evicted: tuple[str, dict] | None = None

    async with _lock:
        if len(_sessions) >= BROWSER_POOL_SESSION_LIMIT:
            # ⚠ Предел — это предел памяти, а не отказ работать. Прежде чем
            # отказать, освобождаем самую долго висевшую без прикосновений
            # сессию: пережившие прогоны вкладки накапливаются молча, и на
            # отказ обречены сразу все занятия, а не одно. Отказ при этом
            # остаётся честно: если вытеснять нечего — занято всё.
            evicted = _pool_stale_session()
            if evicted is None:
                raise RuntimeError(f'предел сессий исчерпан: {BROWSER_POOL_SESSION_LIMIT}')
            _sessions.pop(evicted[0], None)

        session_id = uuid.uuid4().hex[:12]
        options = {
            'viewport': viewport or {'width': BROWSER_POOL_VIEWPORT_WIDTH,
                                     'height': BROWSER_POOL_VIEWPORT_HEIGHT},
            'storage_state': storage_state,
            # Приём скачанного включён явно, хотя таково и умолчание Playwright:
            # на него опирается `browser_file`, и молчаливая смена умолчания в
            # библиотеке сломала бы шаг `download` без единой ошибки в коде.
            'accept_downloads': True,
        }
        if locale:
            options['locale'] = locale
        if timezone_id:
            options['timezone_id'] = timezone_id
        if proxy:
            options['proxy'] = _proxy_options(proxy)
        if user_agent:
            options['user_agent'] = user_agent
        headers = _pool_headers(extra_headers)
        if headers:
            # ⚠ Заголовки на контекст, а не на запрос: их должны нести и
            # переходы внутри сайта, и подзапросы страницы, иначе первый же
            # XHR уедет с умолчанием образа и выдаст подмену.
            options['extra_http_headers'] = headers

        context = await _browser.new_context(**options)

        # ⚠⚠ Маскировка вешается на КОНТЕКСТ и до первой страницы: скрипт
        # должен отработать раньше любого кода сайта, иначе тот успеет
        # спросить `navigator.platform` до подмены. `add_init_script`
        # выполняется при каждой навигации, включая переходы внутри сайта.
        mask = _pool_mask(user_agent)
        if mask:
            await context.add_init_script(mask)

        page = await context.new_page()

        _sessions[session_id] = {
            'session_id': session_id,
            'name': name or session_id,
            'site_id': int(site_id or 0),
            'context': context,
            'page': page,
            'created_at': time.time(),
            'used_at': time.time(),
        }

    # Выход пишем в строку журнала, но **без пароля**: `http_proxy_hide` оставляет
    # схему, логин и хост — по ним видно, той ли прокси открылась сессия.
    through = f', выход {http_proxy_hide(proxy)}' if proxy else ''
    logger_info(f'[browser] сессия открыта: {session_id} ({name or "без имени"})'
                f'{through}, всего {len(_sessions)}')

    if evicted is not None:
        # ⚠ Гасим жертву уже после открытия: реестр мы освободили под замком,
        # а закрытие контекста — долгое, и держать из-за него замок значило бы
        # поставить всю очередь открытия на паузу из-за чужой вкладки.
        victim_id, victim = evicted
        logger_info(f'[browser] сессия {victim_id} вытеснена для места: "{victim["name"]}", '
                    f'без прикосновений {(time.time() - victim["used_at"]) / 60:.0f} мин')
        await _pool_close_context(victim, victim_id)

    if url:
        try:
            await browser_pool_session_goto(session_id, url)
        except Exception as e:
            # ⚠ **Сессию за собой убираем.** Опечатка в адресе и недоступный хост —
            # обычное дело, а у вызывающего на руках ещё нет её номера: погасить её
            # ему нечем, и каждая такая попытка съедала бы место в пуле до
            # перезапуска контейнера. Живьём так и было: 500 в ответ и незакрытая
            # вкладка на `chrome-error://chromewebdata/`.
            #
            # ConnectionError, а не исходное исключение Playwright: «страница не
            # открылась» — это сбой чужого хоста, и API отвечает на него 502, как
            # и на отдельном переходе.
            await browser_pool_session_close(session_id)
            raise ConnectionError(f'страница не открылась: {e}')

    return _session_info(_sessions[session_id])


async def browser_pool_session_close(session_id: str) -> bool:
    """Закрывает сессию. Возвращает False, если такой сессии не было."""
    async with _lock:
        entry = _sessions.pop(session_id, None)

    if entry is None:
        return False

    # Закрытие контекста закрывает и страницы в нём; падать на уже мёртвом
    # контексте (браузер уронил вкладку) незачем — запись из реестра уже снята.
    await _pool_close_context(entry, session_id)

    logger_info(f'[browser] сессия закрыта: {session_id}, осталось {len(_sessions)}')
    return True


def browser_pool_session_get(session_id: str) -> dict | None:
    """Внутренняя запись сессии (со страницей и контекстом) или None.

    ⚠ Одно то, что запись спросили, уже считается прикосновением: обновляет
    `used_at`. Клики и чтение в сессию не ходят через `goto`, и без отметки здесь
    сессия, которой ровно сейчас работают через API, выглядела бы холостой —
    обход по сроку срезал бы её на ходу.
    """
    entry = _sessions.get(session_id)
    if entry is not None:
        entry['used_at'] = time.time()

    return entry


def browser_pool_session_list() -> list[dict]:
    """Список сессий для API — без объектов Playwright."""
    return [_session_info(entry) for entry in _sessions.values()]


async def browser_pool_session_goto(session_id: str, url: str, wait_until: str = 'domcontentloaded',
                                    timeout_ms: int = BROWSER_POOL_GOTO_TIMEOUT_MS) -> dict:
    """Переводит страницу сессии на URL."""
    entry = _sessions.get(session_id)
    if entry is None:
        raise KeyError(session_id)

    page = entry['page']
    if page.is_closed():
        raise RuntimeError('страница сессии закрыта')

    await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    entry['used_at'] = time.time()

    return {**_session_info(entry), 'title': await page.title()}


async def browser_pool_storage_state(session_id: str) -> dict:
    """Cookie и localStorage сессии — чтобы открыть следующую уже авторизованной."""
    entry = _sessions.get(session_id)
    if entry is None:
        raise KeyError(session_id)

    return await entry['context'].storage_state()


def browser_pool_pages(entry: dict) -> list:
    """Живые страницы сессии: первая — сама сессия, дальше всплывшие дочерние.

    Отдельной функцией, потому что спрашивают её все, кто ставит слушателей на
    каждую страницу сессии (запись, инспектор, наблюдатель DOM, журнал консоли), и
    у каждого своя копия этого обхода расходилась бы с прочими на первой правке.
    """
    try:
        return browser_pool_context_pages(entry['context'])
    except Exception:
        return []


def browser_pool_context_pages(context) -> list:
    """То же, но от самого контекста — для тех, у кого записи сессии под рукой нет
    (живое окно работает с контекстом, а не с реестром).

    Закрытый контекст — пустой список, а не исключение: страниц могло не остаться
    к моменту, когда слушатель до них добрался.
    """
    try:
        return [page for page in context.pages if not page.is_closed()]
    except Exception:
        return []


def browser_pool_stat() -> dict:
    """Сводка по пулу: запущен ли браузер и сколько сессий из предела занято."""
    return {
        'running': _browser is not None,
        'version': _browser.version if _browser is not None else '',
        'sessions': len(_sessions),
        'session_limit': BROWSER_POOL_SESSION_LIMIT,
    }


def _session_info(entry: dict) -> dict:
    """Запись сессии в виде, пригодном для JSON.

    `page.url` — свойство, а не запрос в браузер: список сессий не должен зависать
    на странице, которая занята своим JavaScript. По той же причине здесь нет
    `title()` — он уходит в браузер и ждёт ответа.
    """
    page = entry['page']
    now = time.time()

    return {
        'session_id': entry['session_id'],
        'name': entry['name'],
        'site_id': entry.get('site_id', 0),
        'url': page.url,
        'alive': not page.is_closed(),
        'uptime_sec': round(now - entry['created_at']),
        # Календарное время открытия, помимо `uptime_sec`. Это не дубль: «жива
        # 17 часов» отвечает на вопрос «давно ли», а «открыта в 06:27» — на вопрос
        # «когда», и по нему вкладку сопоставляют с тем, что тогда делали.
        'opened_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry['created_at'])),
        'idle_sec': round(now - entry['used_at']),
        # Соседи кладут своё состояние в эту же запись (см. докстринг модуля).
        # Здесь — только факты «идёт запись», «висит наблюдатель», для кнопок в
        # списке: сами накопленные данные спрашивают у своих ручек.
        'recording': entry.get('record') is not None,
        'watching': entry.get('watch') is not None,
    }


async def _idle_sweep():
    """Закрывает сессии, к которым никто не обращался дольше срока.

    Живёт рядом с пулом с самого `browser_pool_start` и ходит минутой за
    минутой. Заводить для этого круг в каждом вызывающем проекте — завести
    круг на каждый проект, а дыра переживших прогоны вкладок у всех одна.
    """
    while True:
        await asyncio.sleep(BROWSER_POOL_SWEEP_SEC)

        now = time.time()
        stale = [(session_id, entry) for session_id, entry in _sessions.items()
                 if now - entry['used_at'] > BROWSER_POOL_IDLE_SEC
                 and not _pool_busy(entry)]

        for session_id, entry in stale:
            logger_info(f'[browser] сессия {session_id} закрыта по холостому ходу: '
                        f'"{entry["name"]}", {round((now - entry["used_at"]) / 60)} мин '
                        'никто не трогал')
            await browser_pool_session_close(session_id)


def _pool_busy(entry: dict) -> bool:
    """Есть ли на сессии живое слушательское состояние — такое не закрывается.

    Запись, наблюдение DOM, журнал консоли и перехват сети накапливаются
    между походами (`net_watch` в одном, `net_dump` в другом) и живут в записи
    сессии нарочно, даже когда к сессии никто не обращается. Снять их молча —
    обрезать чужую работу; такие сессии доживают до закрытия руками.
    """
    net = (entry.get('scenario_watch') or {}).get('net') or {}

    return (entry.get('record') is not None or entry.get('watch') is not None
            or entry.get('console') is not None or net.get('handler') is not None)


def _pool_stale_session() -> tuple[str, dict] | None:
    """Самая долго висящая без прикосновений сессия — жертва при переполнении.

    None — жертвы нет: всё занято работой или слушателями, и тогда переполнение
    отвечает отказом честно.
    """
    now = time.time()
    idle = [(session_id, entry) for session_id, entry in _sessions.items()
            if not _pool_busy(entry)
            and now - entry['used_at'] >= BROWSER_POOL_EVICT_GRACE_SEC]

    if not idle:
        return None

    return min(idle, key=lambda pair: pair[1]['used_at'])


async def _pool_close_context(entry: dict, session_id: str = '') -> None:
    """Гасит контекст записи; закрытый контекст закрывает и свои страницы.

    Исключение — строка в журнал, а не наружу: гасим то, чего реестр уже не
    держит, и падать на этом нечем.
    """
    try:
        await entry['context'].close()
    except Exception as e:
        logger_info(f'[browser] сессия {session_id} закрыта с ошибкой: {e}')


def _pool_headers(extra_headers: dict | None) -> dict:
    """Заголовки отпечатка, годные для ВСЕХ запросов; негодные отброшены.

    ⚠⚠ Самая дорогая ошибка этого модуля, и стоила она дня. Отпечаток
    снимается с НАВИГАЦИИ, и в нём лежат заголовки, осмысленные только для
    неё: `Accept: text/html,…`, `Sec-Fetch-Dest: document`, `Sec-Fetch-Mode:
    navigate`, `Upgrade-Insecure-Requests`. Playwright вешает
    `extra_http_headers` на КАЖДЫЙ запрос контекста — значит и на стили, и на
    скрипты, и на XHR.

    Как это выглядит: страница открывается, но без стилей и без своего JS.
    Разметка на месте, элементы находятся, клики проходят — и не делают
    ничего, потому что обработчики не привязаны. Замер на живом магазине:

        с этими заголовками   4 запроса (одни картинки), корзина пуста
        без них              18 запросов, POST checkout/cart/add 200

    ⚠⚠ Ломает это **`Sec-Fetch-*`, и не на стороне сервера**: Chromium считает
    их своими и подзапрос с заданными вручную не отправляет вовсе. Замер на
    локальном сервере, по заголовку за раз (страница со стилем и скриптом):

        только `Sec-Fetch-Dest`            дошла одна страница, стиль и
        только `Sec-Fetch-Mode`            скрипт — `net::ERR_INVALID_ARGUMENT`
        только `Accept: text/html`         дошло всё
        только `Upgrade-Insecure-Requests` дошло всё

    То есть в сети запроса нет — и в журнале сервера нет тоже. Ни ошибки, ни
    пустого ответа: сценарий честно падал на шаге формы, а виноватой выглядела
    мобильная вёрстка.

    ⚠ `Accept` и `Upgrade-Insecure-Requests` запрос не ломают, но отброшены
    вместе с ними: `Accept: text/html` на стиле и `document` на картинке не
    бывают ни у одного живого браузера — это не выбор зовущего, а протекшая
    подробность снимка, работающая против подмены.

    ⚠ Потери нет: на самой навигации `Sec-Fetch-*` ставит сам Chromium, и
    ставит верно — `document`/`navigate` на странице, `style`/`no-cors` на
    стиле. Зато `Accept-Language` и `Sec-CH-UA*` осмысленны везде и
    остаются — в них весь смысл подмены.
    """
    return {str(k): str(v) for k, v in (extra_headers or {}).items()
            if str(k).lower() not in BROWSER_POOL_HEADERS_SKIP}


def _pool_mask(user_agent: str) -> str:
    """Скрипт, подгоняющий свойства браузера под заявленный UA; пусто — не надо.

    Args:
        user_agent: чем представилась сессия. Пусто — маскировки нет.

    Returns:
        Готовый JS для `add_init_script`; пустая строка — подгонять не к чему.

    ⚠⚠ Пустой UA даёт пустой скрипт, и это главное правило здесь. Сессия без
    подмены представляется честно: Chromium на Linux, `navigator.platform`
    ему отвечает, и противоречия нет. Припиши ей «Win32» — противоречие
    появится там, где его не было, и маскировка сработает против себя.

    ⚠ `platform` выводится из UA, а не задаётся отдельным доводом: два
    независимых источника одного факта расходятся — это вопрос времени, а
    цена расхождения здесь ровно та, от которой лечимся.
    """
    if not user_agent:
        return ''

    low = user_agent.lower()
    if 'android' in low:
        platform = 'Linux armv8l'
    elif 'iphone' in low or 'ipad' in low:
        platform = 'iPhone'
    elif 'windows' in low:
        platform = 'Win32'
    elif 'macintosh' in low or 'mac os x' in low:
        platform = 'MacIntel'
    else:
        # ⚠ Неузнанный UA — не повод выдумывать: `platform` остаётся своим,
        # а `window.chrome` и плагины подправятся. Выдуманная платформа при
        # незнакомой строке вероятнее создаст противоречие, чем уберёт.
        platform = ''

    return BROWSER_POOL_MASK_JS % {'platform': json.dumps(platform)}


def _proxy_options(url: str) -> dict:
    """Адрес выхода → то, чего ждёт Playwright: `{server, username, password}`.

    ⚠ **Логин и пароль отдельными полями, а не внутри адреса.** Playwright
    передаёт `server` в Chromium как есть, а тот учётные данные из URL прокси
    игнорирует: выход молча оказался бы неавторизованным, и провайдер отвечал бы
    407-м на каждый запрос страницы.

    ⚠ **SOCKS5 с паролем Chromium не умеет вовсе** — это его давнее ограничение,
    а не наше. Такой выход годится нашему HTTP-слою (httpx умеет), но не браузеру,
    поэтому случай назван в журнале словами: иначе он выглядит как «сайт не
    открывается», и искать причину будут в сценарии.
    """
    raw = str(url or '')
    scheme, rest = raw.split('://', 1) if '://' in raw else ('http', raw)

    username = password = ''
    if '@' in rest:
        auth, rest = rest.rsplit('@', 1)
        username, _, password = auth.partition(':')
        username, password = unquote(username), unquote(password)

    if username and scheme.startswith('socks'):
        logger_info(f'[browser] ⚠ {scheme} с логином: Chromium не умеет авторизацию SOCKS — '
                    f'сессия пойдёт мимо выхода либо получит отказ провайдера')

    options = {'server': f'{scheme}://{rest}'}
    if username:
        options['username'] = username
    if password:
        options['password'] = password

    return options

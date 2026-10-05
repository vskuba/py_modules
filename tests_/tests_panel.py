"""Клиент к поднятой панели: вход один раз на прогон, клиент свой у теста.

Сюита панели ходит по HTTP в **работающее** приложение, а не поднимает его в
памяти: половина того, что стоит проверять, живёт не в коде — сессия в куке, срез
префикса прокси, права на странице. Тест против объекта `app` их не увидит.

Отсюда и обвязка: войти, запомнить куки, раздать тестам клиентов.

## ⚠⚠ Клиент свой у каждого теста, а сессия общая на прогон

Свой клиент нужен: у него свой пул соединений, и один на все тесты держал бы его
открытым между ними.

А вот входить заново незачем, и это не экономия на спичках. **Замерено:**
`/auth/login` идёт 309 мс против 10 мс у обычного запроса — пароль проверяется
намеренно медленным хешем. Для прода это правильно, здесь разорительно: полторы
тысячи тестов, берущих сессию каждый заново, дают **~470 секунд одних только
входов** при 426 секундах фактического прогона (часть тестов входит не под всеми
ролями). Сессия — подписанная кука, живёт час, а прогон идёт минуты.

## ⚠ Живость куки проверяется, а не предполагается

Кука живёт час-другой, и длинный прогон её переживает. Протухла — входим заново;
без этой проверки сюита падает 401-ми **на последних файлах**, и причину ищут в
правках, которые к делу не относятся.

Проба — дешёвая ручка, которая есть у любой панели: «кто я». Проект, у которого
она зовётся иначе, передаёт свою.

## ⚠⚠ Сам вход берётся из `uvicorn_/uvicorn_panel.py`, а не пишется здесь

Там он и жил: JSON-тело, а не форма — на форму панель отвечает 422. Знание это
одно, и второй копии у него быть не должно.

Разная у них **политика**, а не способ. `uvicorn_panel_client` входит заново на
каждый вызов и сам перелогинивается на 401 — это правильно для разовой проверки
живьём и негодно для сюиты: вход стоит 309 мс (см. выше), а автоперелогин
превратил бы 401 в 200 и у теста, который 401 как раз и проверяет.
"""

import os

import httpx
from uvicorn_.uvicorn_panel import uvicorn_panel_login

# Куда стучаться. ⚠ Из окружения: на проде у панели своё имя, локально порт.
TESTS_PANEL_URL = os.getenv('BASE_URL', 'http://localhost:8000').rstrip('/')

# Сколько ждём ответа. Ручки, которые водят браузер или модель, сюда не входят —
# их сюита не трогает: они стоят минуту и требуют живого соседа.
TESTS_PANEL_TIMEOUT = 30.0

# Чем проверяем, что кука ещё жива. ⚠ Дешёвая ручка общего скелета панели: она
# есть у всех и не трогает предметных таблиц.
TESTS_PANEL_PROBE = '/auth/me'


def tests_panel_client(cookies=None, base_url: str = '',
                        timeout: float = 0) -> httpx.AsyncClient:
    """Клиент к панели. Без кук — им проверяется, что закрытое закрыто.

    Отдаётся **незакрытым**: вызывающий держит его `async with`, как и положено
    фикстуре.

    ```python
    @pytest.fixture
    async def client():
        async with tests_panel_client() as http:
            yield http
    ```

    ⚠ `follow_redirects=False` намеренно: панель отвечает отказом переходом на
    вход, и клиент, идущий по нему, превращает 401 в 200 со страницей входа —
    тест зеленеет, ничего не проверив.
    """
    return httpx.AsyncClient(base_url=base_url or TESTS_PANEL_URL,
                             timeout=timeout or TESTS_PANEL_TIMEOUT,
                             follow_redirects=False,
                             cookies=cookies)


async def tests_panel_login(username: str, password: str, base_url: str = '',
                             probe: str = '') -> httpx.Cookies:
    """Куки роли: берём готовые, а нет — входим один раз на весь прогон.

    Args:
        username: логин.
        password: пароль.
        base_url: адрес панели; пусто — `BASE_URL` из окружения.
        probe: чем проверять живость куки; пусто — «кто я».

    Returns:
        Куки сессии.

    Raises:
        RuntimeError: войти не вышло, с кодом и началом ответа. ⚠ Падаем
            **здесь**, а не в середине проверки: «оператор не может открыть
            бэкапы» одинаково выглядит и когда права работают, и когда учётки
            просто нет.
    """
    url = base_url or TESTS_PANEL_URL
    probe = probe or TESTS_PANEL_PROBE

    cookies = _sessions.get((url, username))
    if cookies is not None:
        async with tests_panel_client(cookies=cookies, base_url=url) as http:
            if (await http.get(probe)).status_code == 200:
                return cookies

    cookies = await uvicorn_panel_login(url, username, password)
    _sessions[(url, username)] = cookies

    return cookies


async def tests_panel_as(username: str, password: str, base_url: str = '',
                          probe: str = '') -> httpx.AsyncClient:
    """Клиент **с сессией** названной учётки. Вход — один раз на прогон.

    ```python
    @pytest.fixture
    async def auth_client():
        async with await tests_panel_as(ADMIN_USERNAME, ADMIN_PASSWORD) as http:
            yield http
    ```
    """
    cookies = await tests_panel_login(username, password, base_url, probe)

    return tests_panel_client(cookies=cookies, base_url=base_url)


def tests_panel_port(base_url: str = '') -> int:
    """Порт панели из её адреса — нужен сторожу сети, чтобы её не запретить."""
    url = base_url or TESTS_PANEL_URL
    tail = (url.rsplit(':', 1) + ['80'])[1]

    return int(tail.split('/')[0])


def tests_panel_forget() -> None:
    """Забыть запомненные сессии. Нужно разовым скриптам, не сюите."""
    _sessions.clear()


# ── Приватное ────────────────────────────────────────────────────────────────

# Куки на прогон, ключ — пара «адрес панели, логин». ⚠ Адрес в ключе потому, что
# один прогон может ходить в две установки, и кука от одной к другой не подойдёт.
_sessions: dict = {}

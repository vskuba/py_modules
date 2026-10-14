"""Один вход в контейнер-браузер для всех, кто его зовёт со стороны проекта.

Адрес, токен и перенос кода ответа нужны и сессиям, и сценариям. Держать это в
одном месте модуля, а не копией в каждом проекте: разошедшиеся копии дают самый
неприятный сорт ошибки — половина запросов ходит с токеном, половина без, и
видно это только на проде, где токен задан.

Сам контейнер ничего не хранит (см. `browser_scenario_run`): хранилище и журнал —
здесь, у вызывающего, у которого есть база.
"""

import httpx
from fastapi import HTTPException

from config.config import config_get

# Имя сервиса из compose, а не `localhost`: петлевой порт контейнера виден только
# с хоста, из соседнего контейнера туда хода нет. Значит — из `.env` каждого
# проекта (`BROWSER_URL`), а не зашито здесь.
BROWSER_UPSTREAM_URL = config_get('BROWSER_URL', 'http://browser:8000')
BROWSER_UPSTREAM_TOKEN = config_get('BROWSER_API_TOKEN', '')

# Переход по URL внутри контейнера ждёт десятки секунд (свой таймаут на шаг),
# запас сверху — чтобы таймаут отработал там, где он осмысленный, и вернул 502
# с причиной, а не оборвался здесь безымянно.
BROWSER_UPSTREAM_TIMEOUT = 60.0

# Сценарий — это до сотни шагов по 10 с каждый, и ждать его надо дольше одного
# перехода. Предел здесь не бережёт контейнер (там свои таймауты на шаг), а не даёт
# запросу висеть вечно, если контейнер замолчал посреди прогона.
BROWSER_UPSTREAM_SCENARIO_TIMEOUT = 600.0


def browser_upstream_headers() -> dict:
    """Заголовок с токеном, если он задан. Не задан — проверки в контейнере нет."""
    return {'X-Browser-Token': BROWSER_UPSTREAM_TOKEN} if BROWSER_UPSTREAM_TOKEN else {}


def browser_upstream_ws_url(path: str) -> str:
    """`http://browser:8000` → `ws://browser:8000` — схема у вебсокета своя."""
    base = BROWSER_UPSTREAM_URL

    if base.startswith('https://'):
        return 'wss://' + base[len('https://'):] + path
    if base.startswith('http://'):
        return 'ws://' + base[len('http://'):] + path

    return base + path


async def browser_upstream_request(method: str, path: str, payload: dict | None = None,
                                   timeout: float = BROWSER_UPSTREAM_TIMEOUT) -> dict:
    """Запрос к контейнеру с прозрачным переносом его кода ответа.

    Код контейнера идёт наружу как есть: «нет такой сессии» должно остаться 404,
    а не превратиться в 502 «браузер недоступен» — это разные поводы для звонка.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, BROWSER_UPSTREAM_URL + path,
                                            json=payload, headers=browser_upstream_headers())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f'контейнер-браузер недоступен: {e}')

    body = response.json() if response.content else {}

    if response.status_code >= 400:
        detail = body.get('detail') if isinstance(body, dict) else None
        raise HTTPException(status_code=response.status_code, detail=detail or 'ошибка контейнера-браузера')

    return body

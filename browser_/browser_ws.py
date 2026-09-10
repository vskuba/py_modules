"""Перехват адреса вебсокета: открыть страницу, поймать подключение, закрыть.

Живой чат сайта ходит вебсокетом, а его адрес собирает JS страницы — в разметке
его нет, и по HTTP не достать (проверено: страница чата отдаёт 8 КБ без единого
упоминания сокета). Зато сам адрес живёт долго и содержит токены сессии, поэтому
браузер нужен один раз: открыли, подсмотрели, закрыли.

Дальше сокет держит обычный клиент из Python. Разница в цене решающая: вкладка
Chromium со страницей чата стоит ~143 МБ, а вебсокет-клиент — десятки килобайт.
Двести учёток в первом виде это 29 ГБ, во втором — единицы мегабайт.

Страница здесь **временная**: своя сессия, своя вкладка, гасится в `finally`.
Держать её открытой незачем — весь смысл в том, чтобы её не держать.
"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from logging_.logging_ import logger_info

from browser_.browser_api import browser_api_auth
from browser_.browser_pool import (browser_pool_session_close, browser_pool_session_get,
                                   browser_pool_session_open)

router = APIRouter()

# Сокет поднимается не сразу: страница чата грузит свой модуль, потом подключается.
# Тридцати секунд хватало во всех замерах, дальше ждать смысла нет.
BROWSER_WS_WAIT_SEC = 30

# Адресов бывает несколько (аналитика, живой чат) — отдаём все, выбирает вызывающий.
BROWSER_WS_LIMIT = 10

# Пауза после прогрева: приложению нужно подняться, иначе переход на чат снова
# происходит «слишком рано».
BROWSER_WS_WARMUP_SEC = 8


class BrowserWsCapture(BaseModel):
    """Что открыть и сколько ждать подключения."""

    url: str = Field(..., description='Страница, на которой поднимается сокет')
    warmup_url: str = Field('', description='Открыть до неё: у некоторых сайтов чат '
                                            'не поднимается с прямого перехода')
    click_selector: str = Field('', max_length=500,
                                description='Нажать вместо перехода: у некоторых сайтов чат '
                                            'открывается только кнопкой')
    session_id: str = Field('', description='Готовая сессия; пусто — заведётся временная')
    storage_state: dict | None = Field(None, description='Вход для временной сессии')
    wait_sec: int = Field(BROWSER_WS_WAIT_SEC, ge=1, le=120)


@router.post('/browser/ws/capture', dependencies=browser_api_auth)
async def browser_ws_capture_post(data: BrowserWsCapture):
    """Открывает страницу и возвращает адреса вебсокетов, которые она подняла.

    Своя сессия гасится всегда, чужая — никогда: её мог открыть человек, и закрыть
    её здесь значило бы отобрать у него страницу.
    """
    own = not data.session_id
    if own:
        try:
            session = await browser_pool_session_open(name='ws-capture',
                                                      storage_state=data.storage_state)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        session_id = session['session_id']
    else:
        session_id = data.session_id

    entry = browser_pool_session_get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail='нет такой сессии')

    page = entry['page']
    found: list[str] = []
    page.on('websocket', lambda ws: found.append(ws.url) if len(found) < BROWSER_WS_LIMIT else None)

    try:
        # Уходим со страницы только если уже стоим на целевой: тот же адрес (а с
        # хешем — тем более) браузер не перезагружает, и уже поднятый сокет второй
        # раз не объявляется — перехват возвращал пусто именно там, где сокет точно
        # был. Если страница другая, уходить незачем: обычный переход даст событие.
        #
        # ⚠ Лишний уход не безобиден. Одностраничное приложение держит вход в
        # памяти страницы: заход на главную (и тем более на `about:blank`)
        # перемонтирует его, и чат после этого не открывается вовсе. Именно на этом
        # перехват и спотыкался.
        if _same_page(page.url, data.url):
            away = data.warmup_url or 'about:blank'
            try:
                await page.goto(away, wait_until='domcontentloaded', timeout=60000)
                await asyncio.sleep(BROWSER_WS_WARMUP_SEC)
            except Exception as e:
                logger_info(f'[browser] уход на {away[:60]} не удался: {e}')

        # Кнопка вместо перехода. Бывает, что чат открывается только так: по
        # прямому адресу страница не поднимает ни чат, ни сокет — проверено и
        # переходом, и перезагрузкой, и заходом через главную.
        if data.click_selector:
            try:
                await page.click(data.click_selector, timeout=20000)
            except Exception as e:
                logger_info(f'[browser] кнопка чата {data.click_selector[:40]}: {e}')
        else:
            await page.goto(data.url, wait_until='domcontentloaded', timeout=60000)

        # Ждём подключения, а не фиксированную паузу: обычно сокет поднимается за
        # 10-20 секунд, и лишнее ожидание — это удержанная вкладка на 143 МБ.
        for _ in range(int(data.wait_sec) * 2):
            if found:
                await asyncio.sleep(2)   # дать подняться соседним сокетам
                break
            await asyncio.sleep(0.5)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'страница не открылась: {e}')
    finally:
        if own:
            await browser_pool_session_close(session_id)

    logger_info(f'[browser] перехват сокета на {data.url[:60]}: найдено {len(found)}')

    return {'sockets': found, 'session_id': '' if own else session_id}


def _same_page(current: str, target: str) -> bool:
    """Одна и та же страница? Хеш не считаем: он не вызывает перезагрузки."""
    return str(current or '').split('#')[0].rstrip('/') == str(target or '').split('#')[0].rstrip('/')

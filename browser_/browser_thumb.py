"""Миниатюра страницы сессии: один кадр, чтобы узнать вкладку в лицо.

Список сессий отвечает на вопрос «сколько их и куда они смотрят», но не на
вопрос «что это». Адрес `chrome-error://chromewebdata/` не говорит, чем была
страница до того, как сломалась, а два десятка строк с одинаковым именем
неразличимы вовсе. Картинка отвечает на это одним взглядом.

## Почему кадр, а не скринкаст

Живое окно (`browser_view`) берёт картинку из CDP-скринкаста: Chromium сам шлёт
JPEG, когда на странице что-то изменилось. Для **одной** страницы это дешевле
цикла со скриншотами.

Здесь всё наоборот: карточек в колонке до `BROWSER_POOL_SESSION_LIMIT`, и держать
столько скринкастов ради неподвижных превью значило бы гонять поток кадров с
двух десятков страниц, на которые никто не смотрит. Поэтому — один снимок по
запросу.

⚠ **Снимок стоит полного рендера страницы.** Поэтому он кэшируется
(`BROWSER_THUMB_TTL_SEC`) и никогда не снимается сам по себе: только когда
карточку показывают, и заново — только по кнопке. Круг обновления списка сессий
миниатюры не трогает.

Отдаём `data:`-строкой внутри JSON, а не картинкой: до контейнера ходят прокси,
который переносит JSON и код ответа как есть. Бинарный ответ потребовал бы
отдельного пути через тот прокси ради двадцати килобайт.

Кадр под свою задачу (вырезка по элементу, страница целиком, PDF) снимает
`browser_capture` — там ни кэша, ни уменьшения: те снимки смотрят по одному.
"""

import asyncio
import time

from logging_.logging_ import logger_info

from browser_.browser_pool import (BROWSER_POOL_VIEWPORT_HEIGHT,
                                   BROWSER_POOL_VIEWPORT_WIDTH,
                                   browser_pool_session_get)

# ⚠ **Кадр уменьшается, а не обрезается.** Снимок области 520×325 из окна 1280×800
# дал бы левый верхний угол страницы крупным планом — по нему вкладку не узнать,
# а именно ради узнавания превью и заводится. Поэтому берём окно целиком и сжимаем
# его втрое: `scale` умеет только CDP, у обёртки Playwright такого параметра нет.
BROWSER_THUMB_SCALE = 0.4

# JPEG и невысокое качество: превью показывает, **что** за страница, а не как она
# свёрстана. PNG того же кадра весит впятеро больше без всякой пользы.
BROWSER_THUMB_QUALITY = 45

# Сколько снятый кадр считается годным. Секунды, а не минуты: страница живая, и
# показывать вчерашний её вид нельзя. Но и не ноль — карточки перерисовываются
# кругом, и без выдержки каждый круг стоил бы полного рендера всех страниц пула.
BROWSER_THUMB_TTL_SEC = 30

# ⚠ Ждём кадр недолго. Страница в пуле бывает занята своим JavaScript намертво —
# ровно такие и интересны в списке, — и снимок с неё не придёт никогда. Список
# карточек не должен из-за одной такой висеть.
BROWSER_THUMB_TIMEOUT_MS = 5000

# Снятые кадры: `session_id` → `(момент, строка data:)`. Монотонные часы: меряется
# «сколько прошло», а не «который час».
_thumbs: dict[str, tuple[float, str]] = {}


async def browser_thumb_get(session_id: str, fresh: bool = False) -> dict:
    """Кадр страницы сессии строкой `data:image/jpeg;base64,…`.

    Args:
        session_id: сессия из пула.
        fresh: снять заново, не дожидаясь конца выдержки. Ставит кнопка на карточке.

    Returns:
        dict: `thumb` — строка `data:` (пустая, если снять не вышло), `cached` —
        отдан ли прежний кадр, `error` — почему не вышло.

    Raises:
        KeyError: нет такой сессии.

    ⚠ **Неудачный снимок — не ошибка ручки.** Страница могла закрыться или зависнуть
    на своём JavaScript; карточка тогда показывает адрес без картинки, и это
    честнее, чем 500 на весь список.
    """
    entry = browser_pool_session_get(session_id)
    if entry is None:
        raise KeyError(session_id)

    hit = _thumbs.get(session_id)
    if hit and not fresh and time.monotonic() - hit[0] < BROWSER_THUMB_TTL_SEC:
        return {'thumb': hit[1], 'cached': True, 'error': ''}

    page = entry['page']
    if page.is_closed():
        return {'thumb': '', 'cached': False, 'error': 'страница закрыта'}

    try:
        data = await asyncio.wait_for(_capture(page), BROWSER_THUMB_TIMEOUT_MS / 1000)
    except Exception as e:
        # Прежний кадр при неудаче не выбрасываем: страница, снятая полминуты
        # назад, узнаётся так же хорошо, а пустое место не говорит ничего.
        logger_info(f'[browser] миниатюра {session_id} не снялась: {_short(e)}')

        return {'thumb': hit[1] if hit else '', 'cached': bool(hit), 'error': _short(e)}

    thumb = 'data:image/jpeg;base64,' + data
    _thumbs[session_id] = (time.monotonic(), thumb)

    return {'thumb': thumb, 'cached': False, 'error': ''}


def browser_thumb_forget(session_id: str) -> None:
    """Забыть кадр погашенной сессии — иначе они копятся до перезапуска.

    Номера сессий не повторяются, поэтому чужой кадр по ошибке не покажется; беда
    была бы только в памяти, но за сутки работы это десятки мегабайт впустую.
    """
    _thumbs.pop(session_id, None)


def _short(error: Exception) -> str:
    """Первая строка ошибки. У Playwright они многострочные, с «call log» на
    полсотни строк — в карточке от него пользы нет."""
    text = str(error).strip().splitlines()

    return (text[0] if text else type(error).__name__)[:200]


async def _capture(page) -> str:
    """Кадр окна целиком, уменьшенный, — уже в base64: его так отдаёт CDP.

    Через CDP, а не `page.screenshot()`, ради `scale` в `clip`: обёртка Playwright
    его не пробрасывает, и уменьшить кадр ею можно только обрезав.

    Сессия CDP заводится на один снимок и закрывается: держать её на каждой
    странице пула — два десятка лишних каналов ради кадра раз в полминуты.
    """
    cdp = await page.context.new_cdp_session(page)
    try:
        shot = await cdp.send('Page.captureScreenshot', {
            'format': 'jpeg',
            'quality': BROWSER_THUMB_QUALITY,
            'clip': {'x': 0, 'y': 0,
                     'width': BROWSER_POOL_VIEWPORT_WIDTH,
                     'height': BROWSER_POOL_VIEWPORT_HEIGHT,
                     'scale': BROWSER_THUMB_SCALE},
        })
    finally:
        await cdp.detach()

    return shot['data']

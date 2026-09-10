"""Снимок страницы: элемент, страница целиком, PDF.

Миниатюра пула (`browser_thumb`) отвечает на вопрос «что это за вкладка» — окно
целиком, мелко, из кэша. Здесь вопросы другие и все три требуют своего кадра:

  * **элемент** — «как выглядит вот эта карточка»: вырезка по селектору, в
    натуральную величину. Так проверяют результат шага и так показывают человеку
    место, на котором сценарий встал;
  * **страница целиком** — с прокруткой, а не только видимая часть: длинная
    переписка или таблица на три экрана иначе обрывается по нижнему краю окна;
  * **PDF** — когда страницу нужно сохранить как документ (счёт, договор,
    выгрузка), а не как картинку.

Отдаётся всё строкой `data:`, а не файлом: снимок обычно едет дальше по HTTP тем
же ответом (в том числе через прокси, который переносит JSON как есть), а часто и
прямо в vision-модель, которая только `data:`-URI и принимает.

⚠ **Разовый кадр страницы по адресу — не сюда, а в `web_shot`** (`web_shot.md`): он
поднимает headless Chrome сам, умеет раздать локальный каталог и не требует ни пула,
ни сессии. Здесь снимают **живую страницу открытой сессии** — ту, в которую уже
вошли и на которой сценарий только что встал; `web_shot` этого не может, потому что
у него своя чистая страница на каждый вызов. Второй признак «сюда»: вырезка по
селектору — `web_shot` режет кадр пиксельными координатами.
"""

import base64

from logging_.logging_ import logger_info

# JPEG и среднее качество: снимок показывает **что** на странице, а не как она
# свёрстана до пикселя. PNG того же кадра весит впятеро больше, и в запрос к
# vision-модели он влезает хуже ровно во столько же раз.
BROWSER_CAPTURE_QUALITY = 70

# Сколько ждём кадр. Страница бывает занята своим JavaScript намертво, и снимок с
# неё не придёт никогда — а ответ «не вышло» нужен вызывающему через секунды, а не
# через минуту.
BROWSER_CAPTURE_TIMEOUT_MS = 15000

# Предел на высоту снимка страницы целиком. Бесконечная лента с подгрузкой растёт,
# пока её листают: без предела кадр вырос бы в десятки мегабайт, которые всё равно
# никто не посмотрит.
BROWSER_CAPTURE_FULL_MAX_HEIGHT = 8000


async def browser_capture_shot(page, selector: str = '', full_page: bool = False,
                               quality: int = BROWSER_CAPTURE_QUALITY,
                               timeout_ms: int = BROWSER_CAPTURE_TIMEOUT_MS) -> dict:
    """
    Кадр страницы или элемента строкой `data:image/jpeg;base64,…`.

    Args:
        page: страница Playwright.
        selector: вырезка по элементу. Пусто — окно (или вся страница, см. ниже).
        full_page: снять страницу целиком, с прокруткой. С `selector` не сочетается
            — у элемента своих границ достаточно, и Playwright такой пары не берёт.
        quality: качество JPEG.
        timeout_ms: сколько ждать элемент и сам кадр.

    Returns:
        dict: `image` — строка `data:`, `bytes` — вес кадра, `error` — почему пусто.

    ⚠ **Неудачный снимок приходит с пустой картинкой и текстом в `error`, а не
    исключением.** Снимок почти всегда снимают, чтобы **разобраться, что не так**:
    шаг упал, страница зависла, элемент не нашёлся. Падать на такой странице значит
    терять единственный способ на неё посмотреть.
    """
    try:
        if selector:
            locator = page.locator(selector).first
            await locator.wait_for(state='visible', timeout=timeout_ms)
            raw = await locator.screenshot(type='jpeg', quality=quality, timeout=timeout_ms)
        else:
            raw = await page.screenshot(type='jpeg', quality=quality, full_page=full_page,
                                        timeout=timeout_ms)
    except Exception as e:
        text = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        logger_info(f'[browser] снимок не снялся: {text}')
        return {'image': '', 'bytes': 0, 'error': text}

    return {'image': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode(),
            'bytes': len(raw), 'error': ''}


async def browser_capture_pdf(page, landscape: bool = False) -> dict:
    """
    Страница как PDF — строкой `data:application/pdf;base64,…`.

    Печать даёт то, чего не даёт картинка: текст остаётся текстом (его можно искать
    и копировать), а разбиение на страницы делает браузер, а не мы обрезкой по
    высоте.

    ⚠ **Работает только в headless-режиме Chromium** — так устроен сам CDP-вызов.
    В обычном окне браузер отвечает отказом, и он приезжает в `error`: контейнер
    поднимается headless, но библиотеку зовут и с машины разработчика.

    `print_background=True` не украшение: без фона исчезают заливки таблиц и
    цветные плашки, по которым в выгрузке и различают строки.
    """
    try:
        raw = await page.pdf(print_background=True, landscape=landscape)
    except Exception as e:
        text = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        logger_info(f'[browser] PDF не собрался: {text}')
        return {'pdf': '', 'bytes': 0, 'error': text}

    return {'pdf': 'data:application/pdf;base64,' + base64.b64encode(raw).decode(),
            'bytes': len(raw), 'error': ''}

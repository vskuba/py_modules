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
#
# ⚠⚠ Константа ЗАЯВЛЯЛАСЬ, но не применялась — предела не было вовсе. Нашлось
# ревизией: лента на три экрана проходит, а лента на сорок отдала бы кадр,
# который не откроется ни в карусели, ни в письме.
BROWSER_CAPTURE_FULL_MAX_HEIGHT = 8000


# Что сделать с липкими и плавающими элементами перед полностраничным кадром.
#
# ⚠⚠ Без этого кадр врёт. Playwright снимает длинную страницу, прокручивая её
# и склеивая куски, — и элемент с `position: sticky` попадает в КАЖДЫЙ кусок:
# шапка магазина оказывается нарисованной посреди формы, закрывая собой поля.
# Поймано живьём: на кадре чекаута шапка перекрыла «Телефон» и «Місто», а
# плавающие кнопки — «Поштовий індекс».
#
# ⚠ Липкое становится обычным (`static`), плавающее — прячется. Разница не
# прихоть: липкая шапка в потоке встаёт туда, где ей и место (вверх страницы),
# а плавающая кнопка, попав в поток, раздвинет вёрстку и сдвинет всё под собой.
#
# ⚠⚠ КРУПНОЕ ПЛАВАЮЩЕЕ НЕ ТРОГАЕМ, и это главное здесь. Модалка — тоже
# `fixed`, и пряталась вместе с шапками: кадр заказа «форма заполнена, кнопка
# не нажата» показывал страницу товара БЕЗ ФОРМЫ — только две кнопки модалки,
# повисшие поверх (у них своя `visibility`). Подпись обещала форму, кадр её не
# содержал. Поймано глазами на готовом кадре, в коде этого не видно.
#
# Отличаем по размеру: украшение (шапка, плашка cookie, кнопка «наверх») либо
# тонкое, либо мелкое; модалка крупна по обоим измерениям. Плюс явные признаки
# диалога — `role=dialog` и `aria-modal`: там, где разметка говорит прямо,
# гадать по пикселям незачем.
BROWSER_CAPTURE_FLAT_JS = """
() => {
    const win = {w: window.innerWidth || 1, h: window.innerHeight || 1};

    // Содержимое, которое кадр обязан показать: большой блок или явный диалог.
    const worthy = (node, box) =>
        node.closest('[role=dialog], [aria-modal=true]') !== null
        || (box.height > win.h * 0.35 && box.width > win.w * 0.25);

    const touched = [];
    document.querySelectorAll('*').forEach(node => {
        const how = getComputedStyle(node).position;
        if (how !== 'sticky' && how !== 'fixed') return;

        if (how === 'fixed' && worthy(node, node.getBoundingClientRect())) return;

        touched.push([node, node.style.cssText]);
        if (how === 'sticky') {
            node.style.setProperty('position', 'static', 'important');
        } else {
            node.style.setProperty('visibility', 'hidden', 'important');
        }
    });
    window.__browserShotTouched = touched;

    return touched.length;
}
"""

# Вернуть странице её собственные стили.
#
# ⚠⚠ Обязательно и всегда: страница ЖИВАЯ, сценарий продолжает на ней работу.
# Оставить шапку выключенной значит сломать следующий шаг, который по ней
# кликает, — и сломать молча, через кадр, который этого шага не касался.
BROWSER_CAPTURE_UNFLAT_JS = """
() => {
    const touched = window.__browserShotTouched || [];
    touched.forEach(([node, css]) => { node.style.cssText = css; });
    window.__browserShotTouched = null;

    return touched.length;
}
"""


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
    flat = full_page and not selector
    try:
        if flat:
            await _capture_flat(page, True)

        if selector:
            locator = page.locator(selector).first
            await locator.wait_for(state='visible', timeout=timeout_ms)
            raw = await locator.screenshot(type='jpeg', quality=quality, timeout=timeout_ms)
        elif full_page:
            raw = await _capture_long(page, quality, timeout_ms)
        else:
            raw = await page.screenshot(type='jpeg', quality=quality,
                                        timeout=timeout_ms)
    except Exception as e:
        text = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        logger_info(f'[browser] снимок не снялся: {text}')
        return {'image': '', 'bytes': 0, 'error': text}
    finally:
        # ⚠⚠ Возврат стилей — в `finally`: снимок мог не выйти, а страница
        # остаётся живой, и сценарий пойдёт по ней дальше. Выключенная шапка
        # сломала бы следующий шаг, который по ней кликает.
        if flat:
            await _capture_flat(page, False)

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


# ── детали реализации ──────────────────────────────────────────────────────

async def _capture_long(page, quality: int, timeout_ms: int) -> bytes:
    """Кадр длинной страницы, обрезанный по `BROWSER_CAPTURE_FULL_MAX_HEIGHT`.

    ⚠⚠ Обрезается ВЕРХНЯЯ часть, а не нижняя: у ленты с подгрузкой смысл
    вверху, а внизу — то, что дозагрузилось, пока снимали. Режет сам
    Playwright через `clip`, одним снимком: собрать всё и отрезать значит
    сперва заплатить за те самые десятки мегабайт, от которых уходим.

    ⚠ Короткая страница снимается как есть — без `clip`, чтобы не зависеть от
    измерения высоты, которое на ней и не нужно.
    """
    try:
        tall = int(await page.evaluate(
            '() => Math.max(document.body.scrollHeight,'
            ' document.documentElement.scrollHeight)') or 0)
        wide = int(await page.evaluate(
            '() => Math.max(document.body.scrollWidth,'
            ' document.documentElement.scrollWidth)') or 0)
    except Exception:  # noqa: BLE001 — не измерили, снимем как есть
        tall = wide = 0

    if tall and wide and tall > BROWSER_CAPTURE_FULL_MAX_HEIGHT:
        logger_info(f'[browser] страница {tall}px обрезана до '
                    f'{BROWSER_CAPTURE_FULL_MAX_HEIGHT}px')

        return await page.screenshot(
            type='jpeg', quality=quality, timeout=timeout_ms,
            clip={'x': 0, 'y': 0, 'width': wide,
                  'height': BROWSER_CAPTURE_FULL_MAX_HEIGHT})

    return await page.screenshot(type='jpeg', quality=quality, full_page=True,
                                 timeout=timeout_ms)


async def _capture_flat(page, on: bool) -> None:
    """Усмирить липкие и плавающие элементы перед кадром — и вернуть обратно.

    Args:
        page: страница Playwright.
        on: `True` — усмирить, `False` — вернуть как было.

    ⚠⚠ Отказ проглатывается молча, и это осознанно: страница могла уйти на
    другой адрес между снимком и возвратом, а снимок — не та работа, ради
    которой стоит ронять прогон. Худшее, что даёт проглоченный отказ, —
    кадр с дублем шапки; худшее, что дало бы исключение, — сорванный заказ.
    """
    try:
        await page.evaluate(BROWSER_CAPTURE_FLAT_JS if on
                            else BROWSER_CAPTURE_UNFLAT_JS)
    except Exception as e:  # noqa: BLE001 — снимок не стоит прогона
        logger_info(f'[browser] липкие элементы не {"убрать" if on else "вернуть"}: '
                    f'{str(e).splitlines()[0][:120]}')

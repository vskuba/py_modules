"""Ввод живого окна: сообщение клиента → настоящее событие в странице.

Ввод идёт через `page.mouse` / `page.keyboard`, а не через `element.click()`:
страница не должна отличать наблюдателя от человека. Так работают hover, фокус,
перетаскивание и обработчики, которые смотрят на `isTrusted`.

Отдельным файлом от самого окна (`browser_view`), потому что растёт отдельно:
окно — это кадры, вкладки и сокет, а здесь разбор событий мыши и клавиатуры, и
пересечения между ними ровно одно — текущая страница.
"""

# Сколько текста принимаем за одну вставку. Буфер обмена бывает и на мегабайт
# (скопировали страницу целиком), а вставка идёт одним сообщением сокета.
BROWSER_VIEW_INPUT_PASTE_LIMIT = 100000

# Умолчания на случай, когда кадра ещё не было и метаданных нет. Совпадают с
# размером окна сессии из пула: клик до первого кадра — редкость, но уехать он
# должен туда, куда человек ткнул, а не в левый верхний угол.
BROWSER_VIEW_INPUT_WIDTH = 1280
BROWSER_VIEW_INPUT_HEIGHT = 800

# Сколько ждём переход, набранный в адресной строке. Столько же, сколько ждёт
# REST-переход пула: разницы для страницы нет, а два разных срока пришлось бы
# объяснять.
BROWSER_VIEW_INPUT_GOTO_TIMEOUT_MS = 30000


async def browser_view_input_apply(page, message: dict, metadata: dict) -> None:
    """Разбирает сообщение клиента в действие над страницей."""
    kind = message.get('type', '')

    if kind == 'mouse':
        x, y = _point(message, metadata)
        action = message.get('action', 'click')
        button = message.get('button', 'left')

        if action == 'move':
            await page.mouse.move(x, y)
        elif action == 'down':
            await page.mouse.move(x, y)
            await page.mouse.down(button=button)
        elif action == 'up':
            await page.mouse.up(button=button)
        else:
            await page.mouse.click(x, y, button=button, click_count=int(message.get('clicks', 1)))

    elif kind == 'wheel':
        x, y = _point(message, metadata)
        await page.mouse.move(x, y)
        await page.mouse.wheel(_number(message.get('dx')), _number(message.get('dy')))

    elif kind == 'key':
        # Строка вида `Control+a` — Playwright сам разложит её на модификаторы.
        key = message.get('key', '')
        if key:
            await page.keyboard.press(key)

    elif kind == 'text':
        text = message.get('text', '')
        if text:
            await page.keyboard.type(text)

    elif kind == 'paste':
        # Буфер обмена у страницы свой, и он пуст: скопировали в системе, а
        # `Control+v` уходит в чужой Chromium, где копировать было нечего.
        # Поэтому текст приезжает вместе с сообщением, а вставляет его
        # `insert_text` — одним событием ввода, как настоящая вставка, а не
        # посимвольным набором, на котором подсказки и автодополнение сходят с ума.
        text = (message.get('text', '') or '')[:BROWSER_VIEW_INPUT_PASTE_LIMIT]
        if text:
            await page.keyboard.insert_text(text)

    elif kind == 'goto':
        url = browser_view_input_url(message.get('url', ''))
        if url:
            await page.goto(url, wait_until='domcontentloaded',
                            timeout=BROWSER_VIEW_INPUT_GOTO_TIMEOUT_MS)

    elif kind == 'back':
        await page.go_back()

    elif kind == 'forward':
        await page.go_forward()

    elif kind == 'reload':
        await page.reload()


def browser_view_input_url(value: str) -> str:
    """Адрес из строки окна. Схему дописываем, а не отвергаем.

    В REST-ручках (`browser_api`) адрес без схемы — ошибка запроса: там его шлёт
    программа, и молча угаданный `https://` скрыл бы её опечатку. Здесь адрес
    набирает человек в адресной строке, и «example.com» он пишет намеренно.
    """
    value = (value or '').strip()
    if value and '://' not in value:
        return f'https://{value}'
    return value


def _number(value, default: float = 0.0) -> float:
    """Число из сообщения клиента: `null` и мусор считаем значением по умолчанию.

    `dict.get(ключ, 0)` тут не спасает: умолчание применяется к отсутствующему
    ключу, а не к присланному `null`. А `null` приходит буднично — клиент делит
    координату на размер картинки, и пока кадра нет, деление на ноль даёт `NaN`,
    который `JSON.stringify` превращает как раз в `null`.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)

    # NaN мимо `float()` проходит, но арифметику после себя обнуляет молча.
    return float(default) if number != number else number


def _point(message: dict, metadata: dict) -> tuple[float, float]:
    """Нормированная точка кадра → CSS-пиксели viewport.

    Клиент шлёт долю ширины и высоты (0..1), а не пиксели: он рисует картинку
    произвольного размера и не обязан знать, во что Chromium её ужал под
    `maxWidth`. Обратный пересчёт делают метаданные последнего кадра —
    `deviceWidth`/`deviceHeight` там и есть viewport в CSS-пикселях.
    """
    width = _number(metadata.get('deviceWidth'), BROWSER_VIEW_INPUT_WIDTH)
    height = _number(metadata.get('deviceHeight'), BROWSER_VIEW_INPUT_HEIGHT)
    offset_top = _number(metadata.get('offsetTop'))

    x = min(max(_number(message.get('x')), 0.0), 1.0) * width
    y = min(max(_number(message.get('y')), 0.0), 1.0) * height + offset_top

    return x, y

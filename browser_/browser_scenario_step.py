"""Одно действие шага над страницей.

Развилка по `action` вынесена из прогона отдельно, потому что растёт она отдельно:
новое умение сценария — это новая ветка здесь и строка в `BROWSER_SCENARIO_ACTIONS`,
а цикл прогона, трасса и работа с сессией при этом не меняются ни на строку.

**Всё идёт через `page.locator`.** Он строгий: два совпадения селектора — это
ошибка, а не «возьмём первое», — и сам ждёт появления элемента до таймаута.
Отдельный `wait_for` перед кликом поэтому не нужен и только удлиняет сценарий.
Строгость оставлена действиям над одним элементом: клик по двум кнопкам сразу и
правда бессмыслен. Действиям над списком (`wait_for`, `extract`) она мешает, и там
берётся `.first` или все совпадения разом.

Состояние прогона (`state`) — это то, что живёт дольше одного шага и должно быть
снято вместе с прогоном: перехват сети и журнал консоли. Держит его прогон, здесь
оно только читается и пополняется.
"""

from browser_.browser_console import (browser_console_rows, browser_console_start,
                                      browser_console_stop)
from browser_.browser_file import browser_file_download, browser_file_upload
from browser_.browser_read import browser_read_page
from browser_.browser_scenario import (BROWSER_SCENARIO_ATTR_MARK,
                                       BROWSER_SCENARIO_FIELDS_DEPTH,
                                       BrowserScenarioStep,
                                       browser_scenario_render)
from browser_.browser_scenario_net import (browser_scenario_net_dump,
                                           browser_scenario_net_start)
from browser_.browser_scenario_rows import browser_scenario_rows_collect
from browser_.browser_wait import (BROWSER_WAIT_QUIET_MS, browser_wait_dom_stable,
                                   browser_wait_text)

# Набор текста посимвольно: 20 мс между клавишами. Мгновенный ввод не замечают
# поля с обработчиком на каждое нажатие — автодополнение, маска телефона.
BROWSER_SCENARIO_STEP_TYPE_DELAY_MS = 20

# Сколько знаков читаемого текста берёт `read_text`. Меньше, чем отдаёт
# `browser_read` по умолчанию: этот текст уезжает в журнал прогона, а там строка
# на двадцать тысяч знаков делает журнал нечитаемым для человека.
BROWSER_SCENARIO_STEP_READ_LIMIT = 8000


async def browser_scenario_step_apply(page, step: BrowserScenarioStep, values: dict,
                                      state: dict | None = None) -> str | list:
    """Выполняет действие шага. Возвращает то, что стоит показать в трассе.

    Список в ответе означает записи: прогон положит их в `extract` и замажет в них
    секреты. Строка — обычное значение шага.
    """
    action = step.action
    timeout = step.timeout_ms
    selector = browser_scenario_render(step.selector, values)
    value = browser_scenario_render(step.value, values)
    state = state if state is not None else {}

    if action == 'goto':
        await page.goto(browser_scenario_render(step.url, values),
                        wait_until='domcontentloaded', timeout=timeout)
        return page.url

    if action == 'click':
        await page.locator(selector).click(timeout=timeout)
        return ''

    if action == 'hover':
        # Наведение, а не клик: подменю и подсказки открываются по `mouseover`, и
        # без этого шага клик по пункту падает раньше, чем пункт появится.
        await page.locator(selector).hover(timeout=timeout)
        return ''

    if action == 'fill':
        await page.locator(selector).fill(value, timeout=timeout)
        return ''

    if action == 'type':
        locator = page.locator(selector)
        await locator.click(timeout=timeout)
        await locator.press_sequentially(value, delay=BROWSER_SCENARIO_STEP_TYPE_DELAY_MS,
                                         timeout=timeout)
        return ''

    if action == 'press':
        key = browser_scenario_render(step.key, values)
        if selector:
            await page.locator(selector).press(key, timeout=timeout)
        else:
            await page.keyboard.press(key)
        return ''

    if action == 'select':
        chosen = await page.locator(selector).select_option(value, timeout=timeout)
        return ', '.join(chosen)

    if action == 'wait_for':
        # `.first` по той же причине, что и в extract: ждут обычно появления списка,
        # а селектор списка совпадает со всеми его строками сразу. Строгий режим
        # Playwright счёл бы это ошибкой — «resolved to 70 elements» вместо ожидания.
        await page.locator(selector).first.wait_for(state='visible', timeout=timeout)
        return ''

    if action == 'wait_url':
        # Шаблон в стиле glob: `**/admin` совпадёт с любым хостом и портом.
        await page.wait_for_url(browser_scenario_render(step.url, values), timeout=timeout)
        return page.url

    if action == 'wait_ms':
        await page.wait_for_timeout(min(int(value), timeout))
        return ''

    if action == 'wait_stable':
        # ⚠ Не дождаться покоя — не сбой шага. Страница с анимацией или бегущими
        # часами не успокоится никогда, и падать на ней значило бы запретить
        # сценарии на половине сайтов. В трассе остаётся, чем кончилось ожидание.
        quiet = int(value) if value.strip().isdigit() else BROWSER_WAIT_QUIET_MS
        result = await browser_wait_dom_stable(page, quiet_ms=quiet, timeout_ms=timeout)
        if result['calm']:
            return f'покой через {result["waited_ms"]} мс, изменений {result["mutations"]}'
        return f'не успокоилась за {result["waited_ms"]} мс, изменений {result["mutations"]}'

    if action == 'wait_text':
        await browser_wait_text(page, value, selector=selector, timeout_ms=timeout)
        return value

    if action == 'scroll':
        await page.mouse.wheel(0, int(value))
        return ''

    if action == 'upload':
        result = await browser_file_upload(page, selector, value, timeout_ms=timeout)
        return ', '.join(result['files'])

    if action == 'download':
        # Путь, а не имя: файл лежит в контейнере браузера, и следующему шагу (или
        # вызывающему) нужен именно путь, чтобы его забрать.
        result = await browser_file_download(page, selector, timeout_ms=timeout)
        return f'{result["path"]} ({result["size"]} байт)'

    if action == 'read_text':
        result = await browser_read_page(page, selector or 'body',
                                         limit=BROWSER_SCENARIO_STEP_READ_LIMIT)
        if result['error']:
            raise RuntimeError(f'текст страницы не прочитался: {result["error"]}')
        return result['text']

    if action == 'extract':
        # Все совпадения, а не первое: собирают обычно список — строки таблицы,
        # пункты меню, карточки. По одному селектору строк несколько, и `inner_text`
        # на них падает строгим режимом Playwright вместо того, чтобы отдать список.
        # Ждать приходится отдельно: `all_inner_texts` не ждёт ничего и на ещё не
        # отрисованном списке молча вернул бы пусто.
        await page.locator(selector).first.wait_for(timeout=timeout)

        # `@src` в значении — снять атрибут, а не текст. Без этого адрес картинки
        # не достать: у `<img>` текста нет, и extract вернул бы пустоту.
        #
        # `@value` — особый случай: читаем свойство, а не атрибут. У поля, заполненного
        # скриптом, атрибута в разметке нет вовсе, и `getAttribute` возвращает пусто,
        # хотя значение на экране видно (`browser_scenario_rows`, та же развилка).
        if value.startswith(BROWSER_SCENARIO_ATTR_MARK):
            name = value[len(BROWSER_SCENARIO_ATTR_MARK):].strip()
            found = await page.locator(selector).evaluate_all(
                '(els, name) => els.map(e => (name === "value" ? (e.value ?? "")'
                '                                              : (e.getAttribute(name) || "")))',
                name)

            return '\n'.join(item.strip() for item in found if item and item.strip())

        texts = [text.strip() for text in await page.locator(selector).all_inner_texts()]

        return '\n'.join(text for text in texts if text)

    if action == 'extract_rows':
        return await browser_scenario_rows_collect(page.locator(selector), step.fields,
                                                   timeout, BROWSER_SCENARIO_FIELDS_DEPTH)

    if action == 'net_watch':
        if state.get('net') is None:
            return 'перехват недоступен'
        return browser_scenario_net_start(page, state['net'], value)

    if action == 'net_dump':
        # Список — значит записи: прогон положит их в `extract` и **замажет
        # секреты**. Это не побочный выигрыш, а условие: в теле запроса входа
        # лежит пароль, и открытым текстом ему в базе места нет.
        return browser_scenario_net_dump(state.get('net'), value)

    if action == 'console_watch':
        # ⚠ Прежний журнал снимаем: второй `console_watch` — это «слушай заново».
        # Иначе на странице повисли бы два набора слушателей, и каждое сообщение
        # записалось бы дважды.
        browser_console_stop(state.get('console'))
        state['console'] = browser_console_start(page, text_filter=value)
        return f'слушаю консоль «{value}»' if value else 'слушаю консоль'

    if action == 'console_dump':
        return browser_console_rows(state.get('console'), text_filter=value)

    if action == 'assert_text':
        text = (await page.locator(selector).inner_text(timeout=timeout)).strip()
        if value not in text:
            raise AssertionError(f'ожидалось «{value}», на странице «{text[:200]}»')
        return text

    raise ValueError(f'неизвестное действие: {action}')

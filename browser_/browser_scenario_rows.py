"""Построчный сбор: список записей со страницы, а не параллельные списки.

Отдельными шагами `extract` поля приходят двумя списками — имена и номера, — и
сшивать их клиенту приходится по позиции. Пропущенное значение сдвигает всё, что
после, и заметить это нельзя: пустые значения из склейки выбрасываются, а перенос
строки внутри ячейки неотличим от разделителя записей. На живом списке карточек
две разные записи носят одинаковое имя сплошь и рядом, так что неверную пару не
отличить и глазами.

Здесь поле привязано к своей строке разметкой, и сшивка структурная: строка
списка — это запись, поля ищутся **внутри неё**.
"""

from browser_.browser_scenario import (BROWSER_SCENARIO_ATTR_VALUE,
                                       BROWSER_SCENARIO_ROWS_LIMIT)

# Сколько ждать чтения одного поля внутри уже найденной строки. Секунды хватает с
# запасом: узел в дереве есть — наличие проверено счётом, — и остаётся прочитать.
# Общий таймаут шага здесь не годится: он умножился бы на число строк.
BROWSER_SCENARIO_ROWS_FIELD_TIMEOUT_MS = 1000


async def browser_scenario_rows_collect(rows, fields, timeout: int, depth: int) -> list:
    """
    Список записей: по строке на совпадение `rows`, поля ищутся внутри строки.

    Ждём появления первой строки, а не каждой: список либо отрисован, либо нет, и
    ожидание по каждому элементу растянуло бы шаг на таймаут за строкой.

    Пустой список — не ошибка: «записей нет» такой же ответ, как «записей
    тринадцать», и падать здесь значило бы заставлять клиента отличать сбой от
    пустоты по тексту.
    """
    try:
        await rows.first.wait_for(timeout=timeout)
    except Exception:
        return []

    total = min(await rows.count(), BROWSER_SCENARIO_ROWS_LIMIT)
    out = []
    for index in range(total):
        row = rows.nth(index)
        record, keep = {}, True
        for field in fields:
            value = await _field_value(row, field, timeout, depth)
            # Обязательное поле пусто — строка подошла под селектор по совпадению:
            # шапка таблицы, разделитель, рекламный блок. Записью её не считаем,
            # иначе отличить такую от настоящей было бы нечем.
            if field.mandatory and not value:
                keep = False
                break
            record[field.name] = value
        if keep:
            out.append(record)

    return out


async def _field_value(row, field, timeout: int, depth: int):
    """
    Значение одного поля внутри строки: текст, атрибут или вложенный список.

    **Внутри строки ничего не ждём.** Строку мы уже дождались, разметка отрисована;
    поля, которого нет сейчас, не будет и через десять секунд. Ожидание здесь не
    просто бесполезно — оно складывается: отсутствующее поле, умноженное на число
    строк, даёт шаг длиной в часы. Поймано на живой странице: пробный сбор с широким
    селектором строки не уложился и в две минуты. Поэтому наличие проверяем счётом, а
    читаем с коротким сроком — узел уже в дереве.

    Отсутствие элемента — пустая строка, а не отказ: у поля без пометки
    `mandatory` пропуск законен, и половина карточек в списке обычно чем-нибудь да
    отличается от другой половины.

    Глубину считаем и здесь, хотя схема её уже проверила: сбор вызывается
    рекурсивно, и второй счётчик стоит дешевле, чем доверие к тому, что наверху
    ничего не поменяется.
    """
    if field.fields and depth > 0:
        return await browser_scenario_rows_collect(
            row.locator(field.selector), field.fields, timeout, depth - 1)

    found = row.locator(field.selector)
    try:
        if await found.count() == 0:
            return ''

        target = found.first
        if field.attribute == BROWSER_SCENARIO_ATTR_VALUE:
            # Значение поля формы, а не атрибут разметки: см. пояснение у константы.
            return (await target.input_value(
                timeout=BROWSER_SCENARIO_ROWS_FIELD_TIMEOUT_MS) or '').strip()

        if field.attribute:
            return (await target.get_attribute(
                field.attribute, timeout=BROWSER_SCENARIO_ROWS_FIELD_TIMEOUT_MS) or '').strip()

        return (await target.inner_text(
            timeout=BROWSER_SCENARIO_ROWS_FIELD_TIMEOUT_MS)).strip()
    except Exception:
        return ''

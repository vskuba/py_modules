"""
Карта экрана: дерево элементов от uiautomator и поиск цели в нём.

Зрение (`adb_screen_describe`) отвечает «что нарисовано», но не даёт координат —
модель их не знает, а угадывать по картинке значит попадать в соседнюю кнопку.
Дамп uiautomator знает точные границы каждого элемента, стоит одной команды и не
тратит ход модели; `adb_input` берёт цель отсюда.

Куда карта не достаёт: экраны на Canvas и WebView отдают дерево без текста — там
остаётся зрение. Обратное тоже верно: элемент нашёлся в дампе — модель спрашивать
незачем.
"""
import argparse
import re
import time
import xml.etree.ElementTree as ET

from adb_.adb_ import ADB_TIMEOUT, adb_run

# Дамп срывается, когда экран анимируется («could not get idle state»): попытки
# берут не числом, а паузой между ними — переход между экранами короче секунды.
ADB_UI_TRIES = 3
ADB_UI_TRY_PAUSE = 0.7

# Ожидание элемента: потолок и шаг опроса. Шаг заметно больше паузы выше — сам
# дамп идёт полсекунды, опрашивать чаще нечем.
ADB_UI_WAIT_TIMEOUT = 15.0
ADB_UI_WAIT_STEP = 1.0


def adb_ui_dump(serial: str = '', tries: int = ADB_UI_TRIES) -> str:
    """
    Сырой XML дерева элементов текущего экрана.

    Args:
        serial: устройство; пусто — единственное подключённое.
        tries: попыток, пока экран не замрёт.

    Returns:
        XML от `<?xml` до `</hierarchy>`, без служебного хвоста утилиты.

    Raises:
        RuntimeError: дамп не удался за все попытки.
    """
    last = ''
    for attempt in range(max(1, tries)):
        if attempt:
            time.sleep(ADB_UI_TRY_PAUSE)
        out = adb_run('exec-out', 'uiautomator', 'dump', '/dev/tty',
                      serial=serial, timeout=ADB_TIMEOUT)
        if '</hierarchy>' in out:
            return _xml_slice(out)
        last = out.strip()[:200]
    raise RuntimeError(f'uiautomator dump: дерево не получено ({last or "пустой ответ"}) — '
                       f'экран анимируется или окно защищено')


def adb_ui_nodes(serial: str = '', xml: str = '') -> list[dict]:
    """
    Элементы экрана списком: текст, класс, границы и точка нажатия.

    Args:
        serial: устройство; пусто — единственное подключённое.
        xml: готовый дамп от `adb_ui_dump`; пусто — снять свой.

    Returns:
        Список словарей `{'text', 'desc', 'id', 'class', 'package', 'clickable',
        'scrollable', 'edit', 'enabled', 'bounds', 'tap'}`. Нажимать — по `tap`:
        это не середина `bounds`, а точка, где нажатие принимают (см. `_tap_point`).

    Raises:
        RuntimeError: дамп не получен или не разобран как XML.
    """
    try:
        root = ET.fromstring(xml or adb_ui_dump(serial=serial))
    except ET.ParseError as err:
        raise RuntimeError(f'uiautomator dump: XML не разобран: {err}')
    parents = {child: elem for elem in root.iter() for child in elem}
    return [node for node in (_node_read(elem, parents) for elem in root.iter('node')) if node]


def adb_ui_find(query: str, serial: str = '', nodes: list = None,
                exact: bool = False) -> dict:
    """
    Найти элемент по надписи — и заодно точку, куда по нему нажимать.

    Ищет в тексте, в подписи для доступности (`content-desc`) и в `resource-id`,
    без учёта регистра. Совпадение целиком старше вхождения, текст старше подписи;
    при равенстве выигрывает меньший по площади — надпись, а не обёртка вокруг.
    Элемент без собственных границ (ярлык вкладки) в этом споре последний: он
    виден на экране, но чем именно — известно только его предку.

    Args:
        query: искомая надпись или её часть.
        serial: устройство; пусто — единственное подключённое.
        nodes: готовый список от `adb_ui_nodes`; пусто — снять свой.
        exact: только полное совпадение.

    Returns:
        Словарь элемента (см. `adb_ui_nodes`) или пустой — не нашлось.
    """
    found = nodes if nodes is not None else adb_ui_nodes(serial=serial)
    needle = query.strip().lower()
    ranked = []
    for node in found:
        rank = _match_rank(node, needle, exact)
        if rank is not None:
            ranked.append((rank, _area(node['bounds']), node))
    if not ranked:
        return {}
    return min(ranked, key=lambda item: (item[0], item[1]))[2]


def adb_ui_wait(query: str, serial: str = '', timeout: float = ADB_UI_WAIT_TIMEOUT,
                exact: bool = False) -> dict:
    """
    Дождаться элемента на экране и вернуть его.

    Экран после нажатия перерисовывается не мгновенно: без ожидания следующий шаг
    ищет цель на старом экране и не находит.

    Args:
        query: искомая надпись или её часть.
        serial: устройство; пусто — единственное подключённое.
        timeout: секунды на ожидание.
        exact: только полное совпадение.

    Returns:
        Словарь элемента (см. `adb_ui_nodes`).

    Raises:
        TimeoutError: элемент не появился за `timeout`.
    """
    deadline = time.monotonic() + timeout
    while True:
        node = adb_ui_find(query, serial=serial, exact=exact)
        if node:
            return node
        if time.monotonic() >= deadline:
            raise TimeoutError(f'элемент «{query}» не появился за {timeout:g} с')
        time.sleep(ADB_UI_WAIT_STEP)


def adb_ui_text(serial: str = '', nodes: list = None) -> str:
    """
    Экран текстом: первой строкой приложение, дальше по строке на цель —
    координата нажатия, надпись и что с целью можно сделать.

    Строки с одной точкой нажатия склеены: одна кнопка живёт в дереве тремя
    узлами (иконка, надпись, стрелка), и порознь они только засоряют карту.
    Порядок — сверху вниз, как экран и читают.

    Args:
        serial: устройство; пусто — единственное подключённое.
        nodes: готовый список от `adb_ui_nodes`; пусто — снять свой.

    Returns:
        Многострочный текст; пустая строка — на экране нет ни надписей, ни целей
        (Canvas или WebView; тогда остаётся `adb_screen_describe`).
    """
    found = nodes if nodes is not None else adb_ui_nodes(serial=serial)
    if not found:
        return ''
    targets = {}
    for node in found:
        _target_merge(targets.setdefault(node['tap'], {}), node)
    lines = [_package_main(found)]
    for tap in sorted(targets, key=lambda point: (point[1], point[0])):
        target = targets[tap]
        if not target['labels'] and not target['clickable']:
            continue
        label = ' · '.join(target['labels']) or f"<{target['class'].rpartition('.')[2]}>"
        lines.append('[{:>4},{:>4}] {:<40} {}'.format(
            tap[0], tap[1], label.replace('\n', ' ⏎ ')[:40], _marks_text(target)).rstrip())
    return '\n'.join(lines)


def _node_read(elem, parents: dict) -> dict:
    """Узел XML в словарь; None — нажимать некуда ни в нём, ни у его предков."""
    tap = _tap_point(elem, parents)
    if not tap:
        return None
    return {
        'text': elem.get('text', ''),
        'desc': elem.get('content-desc', ''),
        'id': elem.get('resource-id', ''),
        'class': elem.get('class', ''),
        'package': elem.get('package', ''),
        'clickable': elem.get('clickable') == 'true',
        'scrollable': elem.get('scrollable') == 'true',
        'edit': 'EditText' in elem.get('class', ''),
        'enabled': elem.get('enabled') == 'true',
        'bounds': _bounds_parse(elem.get('bounds', '')),
        'tap': tap,
    }


def _tap_point(elem, parents: dict) -> tuple:
    """
    Куда нажимать, чтобы сработал этот элемент.

    Надпись сама нажатий не принимает — их принимает строка вокруг неё, поэтому
    сперва ищем ближайшего предка с `clickable`. Такого нет вовсе (Flutter рисует
    экран одним полотном) — берём центр самой надписи: она внутри своей строки, и
    попадание всё равно верное.

    Отдельный случай — узел с нулевыми границами `[0,0][0,0]`: так подписаны, к
    примеру, ярлыки нижних вкладок. Сам по себе он не цель, но у предка координаты
    настоящие, и надпись достаётся вместе с ними — выбросить такой узел значит
    потерять имена вкладок.
    """
    own, fallback = _center(elem), ()
    node = elem
    for _ in range(10):  # выше десятого предка кликабельность уже про весь экран, а не про цель
        center = _center(node)
        if center:
            if node.get('clickable') == 'true':
                return center
            fallback = fallback or center
        node = parents.get(node)
        if node is None:
            break
    return own or fallback


def _center(elem) -> tuple:
    """Центр элемента; пустой кортеж — у элемента нет площади, попадать некуда."""
    bounds = _bounds_parse(elem.get('bounds', ''))
    if not bounds or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        return ()
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def _match_rank(node: dict, needle: str, exact: bool) -> int:
    """Насколько хорошо узел отвечает запросу: меньше — лучше, None — не отвечает."""
    fields = (node['text'].lower(), node['desc'].lower(), node['id'].lower())
    for pos, value in enumerate(fields):
        if value and value == needle:
            return pos
    if exact:
        return None
    for pos, value in enumerate(fields):
        if value and needle in value:
            return 3 + pos
    return None


def _target_merge(target: dict, node: dict) -> None:
    """
    Сложить узел в цель с той же точкой нажатия.

    Надписи копятся без повторов, свойства складываются по «или»: строка меню
    кликабельна сама, а надпись на ней — нет, и цель наследует свойство от того
    из своих узлов, у которого оно есть.
    """
    label = node['text'] or node['desc']
    labels = target.setdefault('labels', [])
    if label and label not in labels:
        labels.append(label)
    target['class'] = target.get('class') or node['class']
    for flag in ('clickable', 'scrollable', 'edit'):
        target[flag] = target.get(flag, False) or node[flag]
    target['enabled'] = target.get('enabled', True) and node['enabled']


def _package_main(nodes: list) -> str:
    """Чьё окно на экране — самый частый пакет среди узлов; шапка карты."""
    counts = {}
    for node in nodes:
        if node['package']:
            counts[node['package']] = counts.get(node['package'], 0) + 1
    return max(counts, key=counts.get) if counts else 'приложение неизвестно'


def _marks_text(target: dict) -> str:
    """Пометки в конце строки карты — что с этой целью можно сделать."""
    marks = []
    if target['edit']:
        marks.append('ввод')
    elif target['clickable']:
        marks.append('нажать')
    if target['scrollable']:
        marks.append('прокрутка')
    if not target['enabled']:
        marks.append('выключен')
    return ' '.join(marks)


def _area(bounds: tuple) -> int:
    """Площадь элемента для выбора меньшего; у безразмерного она не ноль, а бесконечность — иначе он выигрывал бы любой спор."""
    if len(bounds) != 4 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        return 2 ** 31
    return (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])


def _bounds_parse(value: str) -> tuple:
    """`[110,347][970,479]` в `(110, 347, 970, 479)`; пустой кортеж — не разобрано."""
    found = re.findall(r'-?\d+', value)
    return tuple(int(number) for number in found) if len(found) == 4 else ()


def _xml_slice(out: str) -> str:
    """
    Вырезать XML из вывода команды.

    `uiautomator dump /dev/tty` дописывает в тот же поток строку об успехе
    («UI hierchary dumped to: /dev/tty», опечатка вендорская) — с ней разбор
    XML падает.
    """
    start = out.find('<?xml')
    end = out.rfind('</hierarchy>')
    if start < 0 or end < 0:
        raise RuntimeError('uiautomator dump: в ответе нет дерева')
    return out[start:end + len('</hierarchy>')]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Карта экрана устройства и поиск на ней.')
    parser.add_argument('command', choices=['map', 'find', 'dump'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--exact', action='store_true', help='только полное совпадение (find)')
    parser.add_argument('query', nargs='*', help='искомая надпись (find)')
    ns = parser.parse_args()

    try:
        if ns.command == 'map':
            print(adb_ui_text(serial=ns.serial) or 'на экране нет ни надписей, ни целей')
        elif ns.command == 'dump':
            print(adb_ui_dump(serial=ns.serial))
        else:
            match = adb_ui_find(' '.join(ns.query), serial=ns.serial, exact=ns.exact)
            print(match or 'не найдено')
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')

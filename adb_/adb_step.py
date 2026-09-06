"""
Шаг целиком: действие, ожидание покоя и отчёт о том, что от него изменилось.

Одна проверка «что теперь на экране» состоит из четырёх вызовов подряд — нажать,
подождать, снять дамп, прочитать карту. Порознь они складываются в длинные
однострочники, а забытое ожидание превращает следующий замер в замер прошлого
экрана. Здесь эти четыре собраны в один вызов.

Главное, на что шаг отвечает сам, — **шелохнулся ли экран**. Промах мимо кнопки
снаружи неотличим от попадания: команда возвращается успешно и там, и там. Пока
никто не сравнивает экраны до и после, промахи повторяются вслепую, и десять
подряд стоят десяти кругов.

Цель называют надписью, а не координатой: координата из дампа верна ровно до
следующей прокрутки, а надпись ищется заново на каждом шаге. Координата принята
там, где надписи нет вовсе (`adb_step_tap((540, 1200))`), — но тогда и промах
никто не заметит, кроме сравнения.
"""
import argparse

from adb_.adb_ import adb_capture_save
from adb_.adb_input import adb_input_key, adb_input_scroll, adb_input_tap, adb_input_text
from adb_.adb_state import (adb_state_diff, adb_state_diff_text, adb_state_last,
                            adb_state_read, adb_state_save, adb_state_settle)
from adb_.adb_ui import adb_ui_find, adb_ui_nodes


def adb_step_tap(target, serial: str = '', exact: bool = False, shot: str = '') -> dict:
    """
    Нажать на цель и вернуть отчёт о шаге.

    Args:
        target: надпись элемента (или её часть) либо координата — кортеж
            `(x, y)` или строка `'540 1200'`.
        serial: устройство; пусто — единственное подключённое.
        exact: только полное совпадение надписи.
        shot: путь для снимка экрана после шага; пусто — снимок не делать.

    Returns:
        Словарь шага (см. `adb_step_look`).

    Raises:
        RuntimeError: надписи на экране нет — в тексте ошибки карта того, что
            есть. Гадать координату по снимку не нужно: цель либо в дампе, либо
            её на экране нет.
    """
    nodes = adb_ui_nodes(serial=serial)
    before = adb_state_read(serial=serial, nodes=nodes)
    point, label = _target_point(target, nodes, exact, before)
    adb_input_tap(point[0], point[1], serial=serial)
    action = f'нажатие {label} → [{point[0]},{point[1]}]' if label else f'нажатие [{point[0]},{point[1]}]'
    return _step_after(action, before, serial, shot)


def adb_step_scroll(direction: str = 'down', serial: str = '', shot: str = '') -> dict:
    """
    Прокрутить экран и вернуть отчёт о шаге.

    Жест с побочным действием: длинная протяжка листает не список, а весь экран,
    и это видно только по сравнению. `moved` в сравнении означает конец списка —
    надписи те же, координаты другие.

    Args:
        direction: `down`, `up`, `left` или `right` — куда смотреть дальше.
        serial: устройство; пусто — единственное подключённое.
        shot: путь для снимка экрана после шага; пусто — снимок не делать.

    Returns:
        Словарь шага (см. `adb_step_look`).
    """
    before = adb_state_read(serial=serial)
    adb_input_scroll(direction, serial=serial)
    return _step_after(f'прокрутка {direction}', before, serial, shot)


def adb_step_key(name: str, serial: str = '', shot: str = '') -> dict:
    """
    Нажать системную клавишу и вернуть отчёт о шаге.

    Args:
        name: имя клавиши — `BACK`, `HOME`, `ENTER`, `APP_SWITCH`.
        serial: устройство; пусто — единственное подключённое.
        shot: путь для снимка экрана после шага; пусто — снимок не делать.

    Returns:
        Словарь шага (см. `adb_step_look`).
    """
    before = adb_state_read(serial=serial)
    adb_input_key(name, serial=serial)
    return _step_after(f'клавиша {name.upper()}', before, serial, shot)


def adb_step_text(value: str, serial: str = '', shot: str = '') -> dict:
    """
    Напечатать текст в поле под курсором и вернуть отчёт о шаге.

    Поле выбирают заранее (`adb_step_tap`) — без курсора текст уходит в никуда, и
    сравнение это покажет: экран не изменился.

    Args:
        value: текст; только ASCII (см. `adb_input_text`).
        serial: устройство; пусто — единственное подключённое.
        shot: путь для снимка экрана после шага; пусто — снимок не делать.

    Returns:
        Словарь шага (см. `adb_step_look`).
    """
    before = adb_state_read(serial=serial)
    adb_input_text(value, serial=serial)
    return _step_after(f'ввод «{value}»', before, serial, shot)


def adb_step_look(serial: str = '', shot: str = '') -> dict:
    """
    Просто посмотреть: дождаться покоя и сравнить с прошлым замером.

    Шаг без действия — им начинают работу и им же проверяют, чем кончилось то,
    что делалось руками или другой программой.

    Args:
        serial: устройство; пусто — единственное подключённое.
        shot: путь для снимка экрана; пусто — снимок не делать.

    Returns:
        Словарь `{'action', 'before', 'after', 'diff', 'shot', 'note'}`. `after` —
        замер экрана (`adb_state_read`), он же сохранён на диск для следующего
        шага; `diff` — сравнение (`adb_state_diff`); `note` — почему не вышел
        снимок, если его просили. Читать всё это удобнее через `adb_step_report`.
    """
    return _step_after('замер', adb_state_last(serial=serial), serial, shot)


def adb_step_report(step: dict) -> str:
    """
    Отчёт о шаге словами: что сделано, что изменилось и что теперь на экране.

    Args:
        step: словарь шага.

    Returns:
        Многострочный текст — последней идёт карта экрана.
    """
    lines = [step['action']]
    if not step['after'].get('settled', True):
        lines.append('экран не замер — снято на ходу, карта могла устареть')
    lines.append(adb_state_diff_text(step['diff']) or 'сравнивать не с чем — прошлого замера не было')
    if step['shot']:
        lines.append(f"снимок: {step['shot']}")
    if step['note']:
        lines.append(step['note'])
    lines.append(step['after'].get('map') or 'на экране нет ни надписей, ни целей')
    return '\n'.join(lines)


def _step_after(action: str, before: dict, serial: str, shot: str) -> dict:
    """Общий хвост любого шага: дождаться покоя, запомнить замер, сравнить с прежним."""
    after = adb_state_settle(serial=serial)
    adb_state_save(after, serial=serial)
    path, note = _shot_take(shot, serial) if shot else ('', '')
    return {
        'action': action,
        'before': before,
        'after': after,
        'diff': adb_state_diff(before, after),
        'shot': path,
        'note': note,
    }


def _target_point(target, nodes: list, exact: bool, state: dict) -> tuple:
    """Точка нажатия и подпись цели: координата — как названа, надпись — через поиск в дампе."""
    point = _point_parse(target)
    if point:
        return point, ''
    node = adb_ui_find(str(target), nodes=nodes, exact=exact)
    if not node:
        raise RuntimeError(f'надписи «{target}» на экране нет — вот что есть:\n'
                           f'{state["map"] or "ни надписей, ни целей"}')
    return node['tap'], f'«{target}»'


def _point_parse(target) -> tuple:
    """`(540, 1200)` или `'540 1200'` в пару чисел; пустой кортеж — это надпись, а не координата."""
    parts = target if isinstance(target, (tuple, list)) else str(target).replace(',', ' ').split()
    if len(parts) != 2:
        return ()
    try:
        return int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return ()


def _shot_take(path: str, serial: str) -> tuple:
    """
    Снимок экрана к отчёту; отказ не роняет шаг.

    Действие уже произошло, и терять из-за снимка весь отчёт незачем — причина
    едет в `note` и печатается строкой. Отказ обычно один: в тощем окружении
    `adb_capture` тянет `ai_vision`, а с ним и весь стек моделей.
    """
    try:
        return adb_capture_save(path, serial=serial), ''
    except (RuntimeError, ImportError, OSError) as err:
        return '', f'снимок не сделан: {err}'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Шаг на устройстве: действие, ожидание покоя, отчёт об изменениях.',
        epilog="tap 'Меню' | tap 540 1200 | scroll down | key BACK | text hello | look")
    parser.add_argument('command', choices=['tap', 'scroll', 'key', 'text', 'look'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--shot', default='', help='куда сохранить снимок экрана после шага')
    parser.add_argument('--exact', action='store_true', help='только полное совпадение надписи (tap)')
    parser.add_argument('args', nargs='*', help='цель, направление, клавиша или текст')
    ns = parser.parse_args()

    try:
        value = ' '.join(ns.args)
        if ns.command == 'tap':
            done = adb_step_tap(value, serial=ns.serial, exact=ns.exact, shot=ns.shot)
        elif ns.command == 'scroll':
            done = adb_step_scroll(value or 'down', serial=ns.serial, shot=ns.shot)
        elif ns.command == 'key':
            done = adb_step_key(value, serial=ns.serial, shot=ns.shot)
        elif ns.command == 'text':
            done = adb_step_text(value, serial=ns.serial, shot=ns.shot)
        else:
            done = adb_step_look(serial=ns.serial, shot=ns.shot)
        print(adb_step_report(done))
    except (RuntimeError, TimeoutError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

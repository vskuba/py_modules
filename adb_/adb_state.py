"""
Замер экрана: что видно сейчас, замер ли это покоя и что изменилось с прошлого раза.

Действие на телефоне возвращается мгновенно, а экран рисуется секунды — отсюда
правило «сначала замер, потом следующий шаг». Для него не хватало трёх вещей.

**Покой.** `adb_ui_dump` отдаёт дерево в любой момент, в том числе посреди
анимации: замер, снятый сразу после нажатия, показывает уходящий экран.
`adb_state_settle` ждёт, пока два замера подряд совпадут.

**Сравнение.** Промах мимо кнопки внешне неотличим от попадания — пока не
сравнить экраны до и после. `adb_state_diff` отвечает прямо: не шелохнулся,
сменилось приложение, появились и исчезли надписи, содержимое сдвинулось.

**Память между процессами.** Каждая команда оболочки — свой процесс, и всё, что
он снял, умирает вместе с ним; промежуточный файл на устройстве переписывается
следующим дампом. Последний замер лежит на своей стороне
(`adb_state_save`/`adb_state_last`), и следующему вызову есть с чем сравнивать.
"""
import argparse
import hashlib
import json
import os
import re
import tempfile
import time

from adb_.adb_app import adb_app_current
from adb_.adb_ui import adb_ui_nodes, adb_ui_text

# Ожидание покоя: потолок и пауза между замерами. Сам замер идёт около секунды,
# опрашивать чаще нечем; потолок — на анимации перехода, а не на живую загрузку.
ADB_STATE_SETTLE_TIMEOUT = 8.0
ADB_STATE_SETTLE_STEP = 0.4


def adb_state_read(serial: str = '', nodes: list = None) -> dict:
    """
    Снять состояние экрана: приложение, карта целей и отпечаток.

    Args:
        serial: устройство; пусто — единственное подключённое.
        nodes: готовый список от `adb_ui_nodes`; пусто — снять свой. Передавать
            стоит, когда дамп уже сделан: второй стоит ещё секунды.

    Returns:
        Словарь `{'time', 'package', 'activity', 'map', 'labels', 'fingerprint',
        'settled'}`. `map` — человекочитаемая карта (`adb_ui_text`), `labels` —
        те же строки без координат, для сравнения; `fingerprint` — короткий хеш
        всего перечисленного: равные отпечатки значат, что экран не шелохнулся.

    Raises:
        RuntimeError: дамп не получен — экран анимируется или окно защищено.
    """
    found = nodes if nodes is not None else adb_ui_nodes(serial=serial)
    screen = adb_ui_text(nodes=found)
    app = adb_app_current(serial=serial)
    state = {
        'time': time.time(),
        'package': app.get('package', ''),
        'activity': app.get('activity', ''),
        'map': screen,
        'labels': [_line_label(line) for line in screen.splitlines()[1:]],
        'settled': True,
    }
    state['fingerprint'] = _fingerprint(state)
    return state


def adb_state_settle(serial: str = '', timeout: float = ADB_STATE_SETTLE_TIMEOUT,
                     step: float = ADB_STATE_SETTLE_STEP) -> dict:
    """
    Дождаться, пока экран замрёт, и вернуть его состояние.

    Замер считается покоем, когда два подряд дают один отпечаток. Экран так и не
    замер (крутится индикатор, идёт видео) — возвращается последний с пометкой
    `settled: False`: это не ошибка, а честный ответ «снято на ходу».

    Args:
        serial: устройство; пусто — единственное подключённое.
        timeout: секунды на ожидание покоя.
        step: пауза между замерами.

    Returns:
        Словарь состояния (см. `adb_state_read`).

    Raises:
        RuntimeError: ни один замер не удался — дампа нет вовсе.
    """
    deadline = time.monotonic() + timeout
    previous, failure = {}, None
    while True:
        try:
            state = adb_state_read(serial=serial)
            if previous and previous['fingerprint'] == state['fingerprint']:
                return state
            previous, failure = state, None
        except RuntimeError as err:
            failure = err  # дамп сорвался на анимации — это и есть «ещё не замер»
        if time.monotonic() >= deadline:
            break
        time.sleep(step)
    if failure is not None:
        raise RuntimeError(f'экран не дался замеру за {timeout:g} с: {failure}')
    previous['settled'] = False
    return previous


def adb_state_diff(before: dict, after: dict) -> dict:
    """
    Сравнить два замера: что стало с экраном между ними.

    Args:
        before: замер до действия; пустой — сравнивать не с чем.
        after: замер после действия.

    Returns:
        Словарь `{'same', 'app_changed', 'appeared', 'gone', 'moved', 'from', 'to'}`.
        `same` — отпечаток тот же, экран не шелохнулся (обычно это промах мимо
        цели). `moved` — надписи те же, а координаты другие: прокрутка сработала,
        но ничего нового не показала — так выглядит конец списка.
    """
    was = list((before or {}).get('labels') or [])
    now = list((after or {}).get('labels') or [])
    same = bool(before) and before.get('fingerprint') == after.get('fingerprint')
    appeared, gone = _labels_extra(now, was), _labels_extra(was, now)
    return {
        'same': same,
        'app_changed': bool(before) and _app_of(before) != _app_of(after),
        'appeared': appeared,
        'gone': gone,
        'moved': bool(before) and not same and not appeared and not gone,
        'from': _app_of(before),
        'to': _app_of(after),
    }


def adb_state_diff_text(diff: dict) -> str:
    """
    Сравнение словами — строки для отчёта о шаге.

    Args:
        diff: словарь от `adb_state_diff`.

    Returns:
        Многострочный текст; пусто — сравнивать было не с чем.
    """
    if diff.get('same'):
        return ('экран не изменился — промах мимо цели, элемент не принимает нажатий '
                'или экран ещё не перерисовался')
    lines = []
    if diff.get('app_changed'):
        lines.append(f"приложение: {diff.get('from') or '—'} → {diff.get('to') or '—'}")
    if diff.get('appeared'):
        lines.append('появилось: ' + ' · '.join(diff['appeared'][:12]))
    if diff.get('gone'):
        lines.append('исчезло: ' + ' · '.join(diff['gone'][:12]))
    if diff.get('moved'):
        lines.append('надписи те же, координаты другие — содержимое сдвинулось, нового нет')
    return '\n'.join(lines)


def adb_state_save(state: dict, serial: str = '') -> str:
    """
    Запомнить замер на диске — чтобы следующему процессу было с чем сравнивать.

    Args:
        state: словарь состояния.
        serial: устройство; у каждого свой файл.

    Returns:
        Путь файла.
    """
    path = _state_path(serial)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False)
    return path


def adb_state_last(serial: str = '') -> dict:
    """
    Прочитать последний запомненный замер.

    Args:
        serial: устройство; пусто — единственное подключённое.

    Returns:
        Словарь состояния; пустой — замеров ещё не было или файл испорчен.
    """
    try:
        with open(_state_path(serial), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _line_label(line: str) -> str:
    """Строка карты без координат: `[ 540, 413] Профиль   нажать` → `Профиль нажать`."""
    return ' '.join(line.partition(']')[2].split())


def _fingerprint(state: dict) -> str:
    """
    Короткий хеш экрана — по нему видно, что ничего не изменилось.

    В отпечаток идёт и карта, и приложение: карта одинакова у двух пустых
    экранов разных программ, а разница между ними существенная.
    """
    raw = '{}|{}|{}'.format(state['package'], state['activity'], state['map'])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]


def _labels_extra(one: list, other: list) -> list:
    """Надписи из `one`, которых нет в `other`; повторы считаются штуками, а не как множества."""
    rest, extra = list(other), []
    for label in one:
        if label in rest:
            rest.remove(label)
        elif label:
            extra.append(label)
    return extra


def _app_of(state: dict) -> str:
    """`пакет/активность` одной строкой; пусто — фокуса не было (экран погашен)."""
    package, activity = (state or {}).get('package', ''), (state or {}).get('activity', '')
    return f'{package}/{activity}'.strip('/')


def _state_path(serial: str) -> str:
    """Файл замера для устройства; сериал чистится — он попадает в имя файла."""
    name = re.sub(r'[^A-Za-z0-9_.-]', '_', serial) or 'default'
    return os.path.join(tempfile.gettempdir(), f'adb_state_{name}.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Замер экрана: карта, покой и что изменилось с прошлого замера.',
        epilog='read — снять и запомнить; settle — сперва дождаться покоя')
    parser.add_argument('command', choices=['read', 'settle', 'last'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--timeout', type=float, default=ADB_STATE_SETTLE_TIMEOUT,
                        help='секунды ожидания покоя (settle)')
    ns = parser.parse_args()

    try:
        if ns.command == 'last':
            print(adb_state_last(serial=ns.serial).get('map') or 'замеров ещё не было')
            raise SystemExit(0)
        previous = adb_state_last(serial=ns.serial)
        current = (adb_state_settle(serial=ns.serial, timeout=ns.timeout)
                   if ns.command == 'settle' else adb_state_read(serial=ns.serial))
        adb_state_save(current, serial=ns.serial)
        if not current['settled']:
            print(f'экран не замер за {ns.timeout:g} с — снято на ходу')
        print(adb_state_diff_text(adb_state_diff(previous, current)) or 'сравнивать не с чем')
        print(current['map'] or 'на экране нет ни надписей, ни целей')
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

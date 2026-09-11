"""
Руки: нажатия, жесты, ввод текста и аппаратные клавиши.

Координаты сюда приходят из `adb_ui` — угадывать их по снимку экрана нельзя,
промах в соседнюю кнопку внешне неотличим от попадания. Отсюда и главный вход
пакета: `adb_input_tap_on('надпись')` — найти, дождаться и нажать.

Доли экрана вместо пикселей везде, где жест не привязан к элементу: у телефонов
разные экраны, и зашитая координата означает жест не туда на первом же другом
устройстве.
"""
import argparse
import time

from adb_.adb_ import adb_run, adb_screen_size
from adb_.adb_ui import ADB_UI_WAIT_TIMEOUT, adb_ui_wait

# Длительность жестов, миллисекунды. Нажатие короткое; протяжка должна быть
# длиннее порога распознавания, иначе система принимает её за бросок и экран
# улетает на несколько страниц вместо одной.
ADB_INPUT_SWIPE_MS = 400

# Какую долю экрана берёт один прокрут и сколько отступать от краёв: жест,
# начатый у самого края, перехватывают системные жесты «назад» и «шторка».
ADB_INPUT_SCROLL_PART = 0.5
ADB_INPUT_EDGE = 0.1


def adb_input_tap(x: int, y: int, serial: str = '') -> None:
    """
    Нажать в точку экрана.

    Args:
        x: координата по горизонтали, пиксели.
        y: координата по вертикали, пиксели.
        serial: устройство; пусто — единственное подключённое.
    """
    adb_run('shell', 'input', 'tap', str(int(x)), str(int(y)), serial=serial)


def adb_input_tap_on(query: str, serial: str = '', exact: bool = False,
                     timeout: float = ADB_UI_WAIT_TIMEOUT) -> dict:
    """
    Нажать на элемент по надписи — дождавшись, пока он появится.

    Args:
        query: надпись элемента или её часть.
        serial: устройство; пусто — единственное подключённое.
        exact: только полное совпадение надписи.
        timeout: секунды ожидания элемента.

    Returns:
        Словарь нажатого элемента (см. `adb_ui_nodes`) — по нему видно, во что
        попали на самом деле.

    Raises:
        TimeoutError: элемент не появился за `timeout`.
    """
    node = adb_ui_wait(query, serial=serial, timeout=timeout, exact=exact)
    adb_input_tap(node['tap'][0], node['tap'][1], serial=serial)
    return node


def adb_input_swipe(x1: int, y1: int, x2: int, y2: int, ms: int = ADB_INPUT_SWIPE_MS,
                    serial: str = '') -> None:
    """
    Провести пальцем из точки в точку.

    Args:
        x1: начало по горизонтали.
        y1: начало по вертикали.
        x2: конец по горизонтали.
        y2: конец по вертикали.
        ms: длительность жеста; сотни миллисекунд — прокрутка, больше секунды —
            перетаскивание (долгое нажатие и перенос).
        serial: устройство; пусто — единственное подключённое.
    """
    adb_run('shell', 'input', 'swipe', str(int(x1)), str(int(y1)),
            str(int(x2)), str(int(y2)), str(int(ms)), serial=serial)


def adb_input_scroll(direction: str = 'down', serial: str = '',
                     part: float = ADB_INPUT_SCROLL_PART, ms: int = ADB_INPUT_SWIPE_MS) -> None:
    """
    Прокрутить экран на долю его высоты (или ширины).

    Args:
        direction: куда едет содержимое глазами читателя — `down` (смотреть ниже),
            `up`, `left`, `right`.
        serial: устройство; пусто — единственное подключённое.
        part: доля экрана за один жест, 0..1.
        ms: длительность жеста.

    Raises:
        ValueError: направление не из четырёх.
    """
    width, height = adb_screen_size(serial=serial)
    if direction in ('down', 'up'):
        sign = 1 if direction == 'down' else -1
        start, end = _scroll_span(height, part, sign)
        adb_input_swipe(width // 2, start, width // 2, end, ms=ms, serial=serial)
    elif direction in ('left', 'right'):
        sign = 1 if direction == 'right' else -1
        start, end = _scroll_span(width, part, sign)
        adb_input_swipe(start, height // 2, end, height // 2, ms=ms, serial=serial)
    else:
        raise ValueError(f'направление «{direction}»: ожидается down, up, left или right')


def adb_input_text(value: str, serial: str = '') -> None:
    """
    Ввести текст в поле, которое сейчас в фокусе.

    Поле должно быть выбрано заранее (`adb_input_tap_on`) — команда печатает туда,
    где стоит курсор, и молча теряет текст, если курсора нет нигде.

    Args:
        value: текст; только ASCII — см. `Raises`.
        serial: устройство; пусто — единственное подключённое.

    Raises:
        RuntimeError: в тексте есть символы вне ASCII. `input text` шлёт нажатия
            текущей раскладки, а кириллицы и эмодзи в ней нет: на устройство
            уедут либо пропуски, либо мусор. Ввод не-ASCII делают отдельной
            клавиатурой-приёмником (IME вроде ADBKeyBoard) — молча печатать
            испорченное хуже, чем отказаться.
    """
    if not value.isascii():
        bad = ''.join(sorted({ch for ch in value if not ch.isascii()}))[:20]
        raise RuntimeError(f'input text: не-ASCII символы ({bad}) набрать нечем — '
                           f'нужна клавиатура-приёмник (IME) на устройстве')
    adb_run('shell', 'input', 'text', _text_quote(value), serial=serial)


def adb_input_key(name: str, serial: str = '') -> None:
    """
    Нажать аппаратную или системную клавишу.

    Args:
        name: имя клавиши — `BACK`, `HOME`, `ENTER`, `TAB`, `DEL`, `APP_SWITCH`,
            `WAKEUP`; префикс `KEYCODE_` можно не писать. Числовой код тоже принят.
        serial: устройство; пусто — единственное подключённое.
    """
    adb_run('shell', 'input', 'keyevent', _key_code(name), serial=serial)


def adb_input_wake(serial: str = '') -> None:
    """
    Разбудить экран, если он погашен.

    Замок не снимает: пин-код и рисунок отсюда не вводятся. Вызов повторно
    безвреден — `WAKEUP` на включённом экране ничего не делает (в отличие от
    `POWER`, который его погасит).

    Args:
        serial: устройство; пусто — единственное подключённое.
    """
    adb_input_key('WAKEUP', serial=serial)


def adb_input_stayon(on: bool = True, serial: str = '') -> None:
    """
    Держать ли экран включённым, пока устройство на зарядке (`svc power stayon`).

    Для серий и замеров: без этого экран между действиями уходит в сон, и
    половина серии снимает блокировку. По окончании — `on=False`: телефон с
    вечно горящим экраном разряжается и греется сам, а вместе с этим и
    доверие к замерам.

    Args:
        on: True — не гасить на зарядке, False — штатный таймер (вернуть).
        serial: устройство; пусто — единственное подключённое.
    """
    adb_run('shell', 'svc', 'power', 'stayon', 'true' if on else 'false',
            serial=serial)


def adb_input_wake_full(unlock: bool = True, home: bool = True,
                        stayon: bool = True, tries: int = 5,
                        serial: str = '') -> dict:
    """
    Полный ритуал пробуждения: экран → замок → шторка → домашний экран, с
    проверкой фокуса. `adb_input_wake` будит только экран; этот доводит до
    состояния «можно жать и снимать» на HyperOS/MIUI, где ритуал руками
    повторяется по три раза за сессию.

    Три каприза HyperOS, из-за которых одна команда не работает. Первый —
    AOD (`Dozing`) не реагирует на однократный сигнал: жмём `POWER` циклом,
    пока `mWakefulness` не станет `Awake` (на уже разбуженном экране `POWER`
    не жмём — погасит). Второй — окно `AOD`/`NotificationShade` держит
    `mCurrentFocus` даже когда `mFocusedApp` уже лаунчер, и тогда тапы уходят
    в систему, а не в приложение: `cmd statusbar collapse` помогает не
    всегда, `HOME` с AOD не снимает вовсе — отсюда повтор с паузой и свайп
    вверх (жест, которым живой человек сбрасывает AOD на лаунчер). Третий —
    сон между действиями: `svc power stayon true` на время работы (вернуть
    самому, см. `adb_input_stayon`).

    Args:
        unlock: снять замок (`wm dismiss-keyguard`); пин-код не снимается ничем.
        home: выйти на домашний экран после пробуждения.
        stayon: включить «не гаснуть на зарядке» (обратно — `adb_input_stayon(False)`).
        tries: сколько раз жать `POWER`, ожидая `Awake`.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        {'awake': bool, 'focus': str, 'shade': bool} — `focus` строка
        `mCurrentFocus`, `shade=True` — шторка украла фокус даже после
        повтора (тогда трогать экран рано, читать причину).

    Raises:
        RuntimeError: экран не разбужен за `tries` нажатий POWER.
    """
    awake = False
    for _ in range(max(1, tries)):
        if 'mWakefulness=Awake' in adb_run('shell', 'dumpsys', 'power',
                                           serial=serial, timeout=15):
            awake = True
            break
        adb_input_key('POWER', serial=serial)  # на Dozing/Waking именно POWER
        time.sleep(1.0)
    if not awake:
        raise RuntimeError(f'экран не разбужен за {tries} нажатий POWER — '
                           f'устройство спит глубже или adb не там')
    if stayon:
        adb_input_stayon(True, serial=serial)
    adb_run('shell', 'cmd', 'statusbar', 'collapse', serial=serial, timeout=15)
    if unlock:
        adb_run('shell', 'wm', 'dismiss-keyguard', serial=serial, timeout=15)
        # dismiss-keyguard асинхронный: keyguard опадает ~1.5 с; нажатия
        # внутри анимации съедаются ей и до приложения не доходят.
        time.sleep(1.2)
    if home:
        adb_input_key('HOME', serial=serial)
    focus = _settled_focus(serial)
    for _ in range(2):
        if not _stuck_focus(focus):
            break
        # Первый collapse мог прийтись на анимацию и не поднять фокус со
        # шторки. На AOD-устройствах фокус стоит на окне `AOD`/
        # `NotificationShade` и HOME с AOD не справляется — тогда свайп
        # вверх, то, что делает человек: снимает AOD с keyguard на лаунчер.
        # Жесты идут строго после оседания фокуса, иначе предыдущая анимация
        # съест и этот.
        adb_run('shell', 'cmd', 'statusbar', 'collapse', serial=serial, timeout=15)
        if home:
            adb_input_swipe(*_wake_swipe(serial), serial=serial)
            adb_input_key('HOME', serial=serial)
        time.sleep(0.5)
        focus = _settled_focus(serial)
    return {'awake': True, 'focus': focus, 'shade': 'NotificationShade' in focus}


def _wake_swipe(serial: str) -> tuple:
    """Нижняя треть экрана, вверх: жест «разбудить и домой» для AOD-окна."""
    w, h = adb_screen_size(serial)
    return (w // 2, int(h * 0.8), w // 2, int(h * 0.3))


def _stuck_focus(focus: str) -> bool:
    """Фокус на шторке/AOD или ещё не отдан: экран горит, трогать рано."""
    return not focus or 'NotificationShade' in focus or 'AOD' in focus


def _settled_focus(serial: str, wait: float = 2.5) -> str:
    """Читает `mCurrentFocus`, ожидая до `wait` с, пока он сойдёт на нет-окно."""
    focus = _current_focus(serial)
    deadline = time.monotonic() + wait
    while _stuck_focus(focus) and time.monotonic() < deadline:
        time.sleep(0.5)
        focus = _current_focus(serial)
    return focus


def _current_focus(serial: str) -> str:
    """Строка mCurrentFocus из window-сервиса; пусто — фокуса нет вовсе."""
    for line in adb_run('shell', 'dumpsys', 'window', serial=serial,
                        timeout=15).splitlines():
        if 'mCurrentFocus' in line:
            return line.split(':', 1)[-1].strip()
    return ''


def _scroll_span(size: int, part: float, sign: int) -> tuple[int, int]:
    """
    Начало и конец жеста вдоль одной стороны экрана, с отступом от краёв.

    Отступ не косметический: жест, начатый вплотную к краю, забирает себе система —
    снизу «назад» и «домой», сверху шторка, — и до приложения он не доходит вовсе.
    Поэтому большая доля обрезается по краям, а не выходит за них.
    """
    middle, step = size / 2, size * part / 2
    low, high = size * ADB_INPUT_EDGE, size * (1 - ADB_INPUT_EDGE)
    return (int(min(max(middle + step * sign, low), high)),
            int(min(max(middle - step * sign, low), high)))


def _key_code(name: str) -> str:
    """Имя клавиши в том виде, в каком его ждёт `input keyevent`."""
    code = name.strip().upper().replace(' ', '_')
    if code.isdigit() or code.startswith('KEYCODE_'):
        return code
    return f'KEYCODE_{code}'


def _text_quote(value: str) -> str:
    """
    Текст в виде, который переживёт оболочку устройства.

    Аргументы adb склеивает в одну строку и отдаёт `sh` **на телефоне**, поэтому
    пробелы, кавычки и `$` там разбираются заново — как в любой командной строке.
    Одинарные кавычки снимают вопрос целиком; сама одинарная кавычка внутри
    закрывает строку, поэтому её приходится склеивать через экранированную.
    """
    return "'" + value.replace("'", "'\\''") + "'"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Нажатия, жесты и ввод на устройстве.',
        epilog="tap-on 'надпись' | tap 540 1200 | scroll down | text hello | key BACK | "
               "wake-full [no-unlock no-home no-stayon] | stayon on|off")
    parser.add_argument('command', choices=['tap', 'tap-on', 'swipe', 'scroll', 'text',
                                            'key', 'wake', 'wake-full', 'stayon'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--ms', type=int, default=ADB_INPUT_SWIPE_MS, help='длительность жеста')
    parser.add_argument('args', nargs='*', help='аргументы команды')
    ns = parser.parse_args()

    try:
        if ns.command == 'tap':
            adb_input_tap(int(ns.args[0]), int(ns.args[1]), serial=ns.serial)
        elif ns.command == 'tap-on':
            print(adb_input_tap_on(' '.join(ns.args), serial=ns.serial)['tap'])
        elif ns.command == 'swipe':
            adb_input_swipe(*[int(value) for value in ns.args[:4]], ms=ns.ms, serial=ns.serial)
        elif ns.command == 'scroll':
            adb_input_scroll(ns.args[0] if ns.args else 'down', serial=ns.serial, ms=ns.ms)
        elif ns.command == 'text':
            adb_input_text(' '.join(ns.args), serial=ns.serial)
        elif ns.command == 'key':
            adb_input_key(ns.args[0], serial=ns.serial)
        elif ns.command == 'wake-full':
            print(adb_input_wake_full(
                unlock='no-unlock' not in ns.args, home='no-home' not in ns.args,
                stayon='no-stayon' not in ns.args, serial=ns.serial))
        elif ns.command == 'stayon':
            adb_input_stayon(ns.args[0] not in ('off', 'false', '0'), serial=ns.serial)
        else:
            adb_input_wake(serial=ns.serial)
    except (IndexError, ValueError) as err:
        raise SystemExit(f'ошибка в аргументах: {err}')
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')

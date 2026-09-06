"""
Эмулятор: поднять AVD, не дать уснуть, выключить.

Когда физического телефона нет, устройство поднимается здесь: `adb_emu_up()`
запускает AVD headless и ждёт настоящей загрузки (`sys.boot_completed`), а не
только транспорта — `adb wait-for-device` возвращается задолго до того, как на
экране можно работать.

Второе, для чего этот модуль: эмулятор засыпает и гасит яркость в любой
неподходящий момент, и следующие снимки выходят затемнёнными или чёрными —
проверка принимает битый кадр за настоящий. `adb_emu_ready()` выключает сон и
снимает ключгард; забота о том, что экран вообще загорится, не должна лежать
на каждом, кто снимает экран.

`adb_emu_up` и `adb_emu_kill` переживают вызывающий процесс: эмулятор стартует
в собственной сессии, `adb emu kill` гасит его сам.
"""
import argparse
import os
import subprocess
import tempfile
import time

from adb_.adb_ import ADB_TIMEOUT, adb_devices, adb_run
from adb_.adb_input import adb_input_wake

# Ожидание загрузки AVD, секунды. Холодный старт на слабой машине идёт
# полные минуты; дольше ждать — уже не загрузка, а зависание.
ADB_EMU_BOOT_TIMEOUT = 240.0

# Пауза между опросами `adb devices` и `sys.boot_completed`, секунды.
ADB_EMU_POLL = 2.0

# Сколько ждём исчезновения устройства после `emu kill`, секунды.
ADB_EMU_KILL_TIMEOUT = 30.0

# Флаги headless-запуска: без окна, звука и анимации загрузки — эмулятор нужен
# как устройство для проверок, а не как витрина; swiftshader рисует GPU софтом,
# поэтому кадр одинаков на любой машине.
ADB_EMU_HEADLESS_ARGS = ['-no-window', '-no-audio', '-no-boot-anim',
                         '-gpu', 'swiftshader_indirect']

# Где искать бинарь эмулятора, кроме PATH: стандартные места Android SDK.
ADB_EMU_SDKS = ('ANDROID_HOME', 'ANDROID_SDK_ROOT')


def adb_emu_up(name: str, headless: bool = True,
               timeout: float = ADB_EMU_BOOT_TIMEOUT) -> dict:
    """
    Запустить AVD и дождаться настоящей загрузки (не только транспорта).

    Уже запущенный эмулятор с таким именем — не второй экземпляр, а тот же:
    функция вернёт его сериал.

    Args:
        name: имя AVD (`avdmanager list avd`).
        headless: без окна, звука и анимации — так снимаются проверки.
        timeout: секунды ожидания загрузки.

    Returns:
        {'serial': сериал, 'started': запускали ли мы, 'log': путь к логу}.

    Raises:
        RuntimeError: бинарь эмулятора не найден или AVD не запустился.
        TimeoutError: загрузка не завершилась за timeout.
    """
    serial = _emu_serial(name)
    started = False
    log_path = os.path.join(tempfile.gettempdir(), f'adb_emu_{name}.log')

    if not serial:
        before = set(_emu_serials())
        binary = _emu_binary()
        args = [binary, '-avd', name] + (ADB_EMU_HEADLESS_ARGS if headless else ['-no-audio'])
        with open(log_path, 'ab') as log:
            # start_new_session: эмулятор не должен умереть вместе с тем,
            # кто его поднял, и не подхватить сигналы его терминала.
            subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        started = True
        deadline = time.monotonic() + timeout
        while not serial:
            if time.monotonic() > deadline:
                raise TimeoutError(f'{name}: эмулятор не появился в adb за {timeout} c, лог {log_path}')
            fresh = [s for s in _emu_serials() if s not in before]
            if fresh:
                serial = fresh[0]
            else:
                time.sleep(ADB_EMU_POLL)

    _wait_boot(serial, timeout)
    return {'serial': serial, 'started': started, 'log': log_path}


def adb_emu_ready(serial: str = '') -> None:
    """
    Разбудить экран, отключить сон и снять ключгард.

    Для долгих съёмок и прогонов: заснувший между снимками экран даёт затемнённый
    кадр, и проверка принимает битый снимок за настоящий. `svc power stayon`
    держит экран включённым, `wm dismiss-keyguard` поднимает то, что всё-таки
    закрылось.

    Args:
        serial: устройство; пусто — единственное подключённое.
    """
    adb_input_wake(serial=serial)
    adb_run('shell', 'svc', 'power', 'stayon', 'true', serial=serial)
    adb_run('shell', 'wm', 'dismiss-keyguard', serial=serial)


def adb_emu_sleep(serial: str = '') -> None:
    """
    Вернуть управление питанием системе (отменить `stayon`).

    Выключенный сон на боевом устройстве — оставленный без присмотра разряженный
    экран; после проверок его возвращают.

    Args:
        serial: устройство; пусто — единственное подключённое.
    """
    adb_run('shell', 'svc', 'power', 'stayon', 'false', serial=serial)


def adb_emu_kill(name: str = '', serial: str = '') -> None:
    """
    Выключить эмулятор и дождаться, что он исчез из `adb devices`.

    Args:
        name: имя AVD — эмулятор будет найден среди запущенных по нему.
        serial: сериал; если задан, поиск по имени не нужен.

    Raises:
        RuntimeError: указанный эмулятор не найден среди устройств.
    """
    if not serial:
        serial = _emu_serial(name)
        if not serial:
            raise RuntimeError(f'эмулятор {name or "?"} не запущен')
    adb_run('emu', 'kill', serial=serial, timeout=ADB_TIMEOUT)
    deadline = time.monotonic() + ADB_EMU_KILL_TIMEOUT
    while serial in adb_devices():
        if time.monotonic() > deadline:
            raise TimeoutError(f'{serial}: не исчез из adb за {ADB_EMU_KILL_TIMEOUT} c')
        time.sleep(ADB_EMU_POLL)


def _emu_binary() -> str:
    """Путь к бинарю эмулятора: PATH, затем стандартные каталоги SDK."""
    found = subprocess.run(['which', 'emulator'], capture_output=True, text=True)
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    homes = [os.environ[key] for key in ADB_EMU_SDKS if os.environ.get(key)]
    homes.append(os.path.expanduser('~/Android/Sdk'))
    for home in homes:
        candidate = os.path.join(home, 'emulator', 'emulator')
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError('эмулятор не найден (PATH, ANDROID_HOME, ANDROID_SDK_ROOT, ~/Android/Sdk)')


def _emu_serials() -> list[str]:
    """Сериалы всех эмуляторов среди подключённых устройств."""
    return [s for s in adb_devices() if s.startswith('emulator-')]


def _emu_serial(name: str) -> str:
    """Сериал эмулятора с таким именем AVD; пусто — не запущен."""
    if not name:
        return ''
    for serial in _emu_serials():
        # консоль отвечает «имя\r\nOK» — сравнивать со всей строкой нельзя
        answer = adb_run('emu', 'avd', 'name', serial=serial).split('\n')
        if answer and answer[0].strip() == name:
            return serial
    return ''


def _wait_boot(serial: str, timeout: float) -> None:
    """Ждать `sys.boot_completed == 1`; транспорт adb — ещё не загрузка."""
    deadline = time.monotonic() + timeout
    while adb_run('shell', 'getprop', 'sys.boot_completed', serial=serial).strip() != '1':
        if time.monotonic() > deadline:
            raise TimeoutError(f'{serial}: загрузка не завершилась за {timeout} c')
        time.sleep(ADB_EMU_POLL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Жизненный цикл эмулятора: запуск, сон экрана, выключение.',
        epilog="up reserve_avd | ready | sleep | kill reserve_avd")
    parser.add_argument('command', choices=['up', 'ready', 'sleep', 'kill'])
    parser.add_argument('avd', nargs='?', default='', help='имя AVD')
    parser.add_argument('--serial', default='', help='сериал эмулятора')
    parser.add_argument('--window', action='store_true', help='с окном (по умолчанию headless)')
    ns = parser.parse_args()

    try:
        if ns.command == 'up':
            if not ns.avd:
                raise SystemExit('укажите имя AVD')
            print(adb_emu_up(ns.avd, headless=not ns.window))
        elif ns.command == 'ready':
            adb_emu_ready(serial=ns.serial)
        elif ns.command == 'sleep':
            adb_emu_sleep(serial=ns.serial)
        else:
            adb_emu_kill(name=ns.avd, serial=ns.serial)
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')

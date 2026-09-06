"""
Журнал устройства: logcat кусками, а не потоком.

Экран говорит, что приложение «не работает», журнал — почему. Здесь он
достаётся хвостом и с фильтрами, а не рекой в терминал: `-d` стоит в каждой
команде, потому что logcat без него не завершается никогда и вешает вызывающего
до таймаута.

Полный буфер устройства — это десятки тысяч строк общесистемного шума. Читать
его целиком незачем: фильтр по приложению (`package`) и уровню (`level`)
оставляет то, что имеет отношение к делу.
"""
import argparse

from adb_.adb_ import adb_run

# Сколько строк берём по умолчанию: хвост, который ещё читается глазами и моделью
# целиком. Причина сбоя лежит в последних строках; за большим — растить `lines`.
ADB_LOG_LINES = 200

# Уровни logcat от подробного к тяжёлому — в порядке фильтра `*:<буква>`.
ADB_LOG_LEVELS = ('V', 'D', 'I', 'W', 'E', 'F')


def adb_log_read(serial: str = '', package: str = '', lines: int = ADB_LOG_LINES,
                 level: str = '', tag: str = '') -> str:
    """
    Хвост журнала устройства.

    Args:
        serial: устройство; пусто — единственное подключённое.
        package: оставить только строки этого приложения; требует, чтобы оно
            было запущено, — см. `Raises`.
        lines: сколько последних строк вернуть.
        level: минимальный уровень — `V`, `D`, `I`, `W`, `E`, `F`; пусто — все.
        tag: оставить только этот тег logcat (`ActivityManager`, имя своей метки).

    Returns:
        Строки журнала в формате `-v time`; пустая строка — под фильтр ничего
        не попало.

    Raises:
        ValueError: уровень не из `ADB_LOG_LEVELS`.
        RuntimeError: приложение названо, но не запущено — фильтровать не по
            чему: logcat отбирает строки по номеру процесса, а его нет.
    """
    args = ['logcat', '-d', '-v', 'time', '-t', str(int(lines))]
    if package:
        args += [f'--pid={adb_log_pid(package, serial=serial)}']
    if tag:
        # Своя метка проходит, всё прочее глушится: без хвоста `*:S` фильтр по
        # тегу ничего не отбирает — он лишь добавляет тег к общему потоку.
        args += [f'{tag}:{_level_check(level) if level else "V"}', '*:S']
    elif level:
        args += [f'*:{_level_check(level)}']
    return adb_run('shell', *args, serial=serial)


def adb_log_crash(serial: str = '', lines: int = ADB_LOG_LINES) -> str:
    """
    Буфер падений: трассировки исключений и смерти процессов.

    Отдельный буфер устройства, а не отфильтрованный общий, — в нём лежит только
    то, из-за чего приложение закрылось. Пусто — падений не было (буфер живёт до
    перезагрузки), и это самый быстрый ответ на «оно упало или просто закрылось».

    Args:
        serial: устройство; пусто — единственное подключённое.
        lines: сколько последних строк вернуть.
    """
    return adb_run('shell', 'logcat', '-d', '-b', 'crash', '-v', 'time',
                   '-t', str(int(lines)), serial=serial)


def adb_log_clear(serial: str = '') -> None:
    """
    Очистить буферы журнала.

    Ставится **перед** проверяемым действием: после него в журнале остаётся
    только то, что вызвало само действие, — иначе причину ищут среди тысяч
    посторонних строк.

    Args:
        serial: устройство; пусто — единственное подключённое.
    """
    adb_run('shell', 'logcat', '-c', '-b', 'all', serial=serial)


def adb_log_pid(package: str, serial: str = '') -> int:
    """
    Номер процесса приложения.

    Args:
        package: имя пакета.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        Номер процесса.

    Raises:
        RuntimeError: приложение не запущено.
    """
    out = adb_run('shell', 'pidof', '-s', package, serial=serial).strip()
    if not out.isdigit():
        raise RuntimeError(f'«{package}» не запущен — номера процесса нет, '
                           f'фильтровать журнал не по чему')
    return int(out)


def _level_check(level: str) -> str:
    """Уровень в том виде, в каком его ждёт фильтр logcat."""
    mark = level.strip().upper()[:1]
    if mark not in ADB_LOG_LEVELS:
        raise ValueError(f'уровень «{level}»: ожидается один из {", ".join(ADB_LOG_LEVELS)}')
    return mark


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Журнал устройства: хвост, падения, очистка.',
        epilog='read --package com.example --level E | crash | clear')
    parser.add_argument('command', choices=['read', 'crash', 'clear', 'pid'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--package', default='', help='имя пакета: только его строки')
    parser.add_argument('--lines', type=int, default=ADB_LOG_LINES, help='сколько последних строк')
    parser.add_argument('--level', default='', help='минимальный уровень: V D I W E F')
    parser.add_argument('--tag', default='', help='только этот тег logcat')
    ns = parser.parse_args()

    try:
        if ns.command == 'read':
            print(adb_log_read(serial=ns.serial, package=ns.package, lines=ns.lines,
                               level=ns.level, tag=ns.tag).rstrip() or 'под фильтр ничего не попало')
        elif ns.command == 'crash':
            print(adb_log_crash(serial=ns.serial, lines=ns.lines).rstrip() or 'падений в буфере нет')
        elif ns.command == 'pid':
            print(adb_log_pid(ns.package, serial=ns.serial))
        else:
            adb_log_clear(serial=ns.serial)
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

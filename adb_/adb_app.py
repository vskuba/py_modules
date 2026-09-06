"""
Приложения на устройстве: что открыто, что установлено, запуск и остановка.

Вопрос «а нужный экран вообще открылся?» решается здесь одной командой и без
снимка: `adb_app_current()` называет пакет и активность. Зрение и карта экрана
(`adb_ui`) отвечают, что на экране нарисовано, — но не отличат чужое приложение
поверх нашего от нашего же.

Имя пакета — единственное надёжное имя приложения: заголовок на экране
переводится, меняется от версии и повторяется у разных программ.
"""
import argparse
import re
import time

from adb_.adb_ import adb_run

# Ожидание приложения на переднем плане: холодный старт тяжёлого приложения —
# это секунды, а опрашивать чаще, чем идёт `dumpsys`, бессмысленно.
ADB_APP_WAIT_TIMEOUT = 20.0
ADB_APP_WAIT_STEP = 0.5


def adb_app_current(serial: str = '') -> dict:
    """
    Что сейчас на переднем плане.

    Args:
        serial: устройство; пусто — единственное подключённое.

    Returns:
        `{'package': ..., 'activity': ...}`; пустой словарь — фокуса нет вовсе
        (экран погашен, замок, шторка поверх всего). Это не ошибка: телефон
        имеет право лежать выключенным.

    Raises:
        RuntimeError: adb подвёл.
    """
    out = adb_run('shell', 'dumpsys', 'window', serial=serial)
    for line in out.splitlines():
        if 'mCurrentFocus' not in line and 'mFocusedApp' not in line:
            continue
        found = _component_parse(line)
        if found:
            return found
    return {}


def adb_app_list(serial: str = '', query: str = '', system: bool = False) -> list[str]:
    """
    Имена установленных пакетов.

    Args:
        serial: устройство; пусто — единственное подключённое.
        query: оставить только пакеты, содержащие эту подстроку.
        system: показывать и системные; по умолчанию только поставленные
            пользователем — системных на телефоне сотни, и в них не ищут.

    Returns:
        Отсортированный список имён пакетов.
    """
    args = ['shell', 'pm', 'list', 'packages'] + ([] if system else ['-3'])
    names = [line.partition(':')[2].strip() for line in adb_run(*args, serial=serial).splitlines()]
    return sorted(name for name in names if name and query.lower() in name.lower())


def adb_app_start(package: str, serial: str = '', activity: str = '') -> str:
    """
    Запустить приложение (или его конкретный экран) и вернуть запущенное.

    Уже открытое приложение выносится на передний план, а не запускается заново.

    Args:
        package: имя пакета.
        serial: устройство; пусто — единственное подключённое.
        activity: активность внутри пакета — `.MainActivity` или полное имя;
            пусто — экран, назначенный приложением стартовым.

    Returns:
        Строка `пакет/активность` для явного запуска, иначе имя пакета.

    Raises:
        RuntimeError: пакета нет на устройстве или запуск отклонён.
    """
    if activity:
        target = f'{package}/{activity}'
        out = adb_run('shell', 'am', 'start', '-n', target, serial=serial)
    else:
        target = package
        # monkey вместо `am start`: стартовую активность он находит сам, а её имя
        # у приложения своё и меняется между версиями.
        out = adb_run('shell', 'monkey', '-p', package,
                      '-c', 'android.intent.category.LAUNCHER', '1', serial=serial)
    _start_check(out, target)
    return target


def adb_app_stop(package: str, serial: str = '') -> None:
    """
    Остановить приложение целиком (`force-stop`).

    Данные и настройки остаются на месте — теряется только несохранённое в
    текущем сеансе, как при закрытии из списка недавних. Открытые приложения
    человека этим не гасят: экран, на который он смотрел, исчезнет.

    Args:
        package: имя пакета.
        serial: устройство; пусто — единственное подключённое.
    """
    adb_run('shell', 'am', 'force-stop', package, serial=serial)


def adb_app_wait(package: str, serial: str = '', timeout: float = ADB_APP_WAIT_TIMEOUT) -> dict:
    """
    Дождаться, пока приложение окажется на переднем плане.

    Ставится сразу за `adb_app_start`: команда запуска возвращается мгновенно, а
    первый экран рисуется секунды — нажатие в этом промежутке уходит в пустоту.

    Args:
        package: имя пакета.
        serial: устройство; пусто — единственное подключённое.
        timeout: секунды ожидания.

    Returns:
        То же, что `adb_app_current`.

    Raises:
        TimeoutError: приложение не вышло вперёд за `timeout`.
    """
    deadline = time.monotonic() + timeout
    current = {}
    while time.monotonic() < deadline:
        current = adb_app_current(serial=serial)
        if current.get('package') == package:
            return current
        time.sleep(ADB_APP_WAIT_STEP)
    seen = current.get('package') or 'фокуса нет'
    raise TimeoutError(f'«{package}» не вышел на передний план за {timeout:g} с (сейчас {seen})')


def adb_app_version(package: str, serial: str = '') -> dict:
    """
    Версия установленного приложения и даты установки.

    Args:
        package: имя пакета.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        `{'package', 'version', 'code', 'installed', 'updated'}`; неизвестные
        поля — пустые строки.

    Raises:
        RuntimeError: пакет на устройстве не найден.
    """
    out = adb_run('shell', 'dumpsys', 'package', package, serial=serial)
    fields = {'package': package}
    for name, pattern in (('version', r'versionName=(\S+)'), ('code', r'versionCode=(\d+)'),
                          ('installed', r'firstInstallTime=(.+)'), ('updated', r'lastUpdateTime=(.+)')):
        found = re.search(pattern, out)
        fields[name] = found.group(1).strip() if found else ''
    if not fields['version'] and not fields['code']:
        raise RuntimeError(f'пакет «{package}» на устройстве не найден')
    return fields


def _component_parse(line: str) -> dict:
    """
    Пакет и активность из строки dumpsys, если они там есть.

    В этих строках лежит то `Window{… пакет/активность}`, то `null`, то системное
    окно без активности (шторка, клавиатура) — из последних брать нечего.
    """
    found = re.search(r'([A-Za-z][\w.]+\.[\w.]+)/([\w.]+)', line)
    if not found:
        return {}
    package, activity = found.group(1), found.group(2)
    # Короткая запись `пакет/.Экран` — своя активность пакета, разворачиваем.
    return {'package': package, 'activity': package + activity if activity.startswith('.') else activity}


def _start_check(out: str, target: str) -> None:
    """
    Запуск удался?

    И `am start`, и `monkey` о провале сообщают **нулевым кодом** и словом в
    выводе — молчаливо верить коду возврата здесь нельзя. Слова у них разные:
    `am` пишет `Error:`, `monkey` — «No activities found to run» (так выглядит
    пакет без стартового экрана: сервис, плагин, опечатка в имени).
    """
    marks = ('error:', 'exception', 'no activities found', 'aborted')
    if any(mark in out.lower() for mark in marks):
        raise RuntimeError(f'запуск «{target}»: {out.strip()[:200]}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Приложения устройства: текущее, список, запуск, остановка, версия.',
        epilog='list --query telegram | start com.example | current')
    parser.add_argument('command', choices=['current', 'list', 'start', 'stop', 'version', 'wait'])
    parser.add_argument('package', nargs='?', default='', help='имя пакета')
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--activity', default='', help='активность для start')
    parser.add_argument('--query', default='', help='подстрока имени для list')
    parser.add_argument('--system', action='store_true', help='включить системные пакеты в list')
    ns = parser.parse_args()

    try:
        if ns.command == 'current':
            found = adb_app_current(serial=ns.serial)
            print('{package} / {activity}'.format(**found) if found else 'переднего плана нет')
        elif ns.command == 'list':
            print('\n'.join(adb_app_list(serial=ns.serial, query=ns.query, system=ns.system)))
        elif ns.command == 'start':
            print(adb_app_start(ns.package, serial=ns.serial, activity=ns.activity))
        elif ns.command == 'stop':
            adb_app_stop(ns.package, serial=ns.serial)
        elif ns.command == 'wait':
            print('{package} / {activity}'.format(**adb_app_wait(ns.package, serial=ns.serial)))
        else:
            print('{package} {version} ({code}), обновлено {updated}'.format(
                **adb_app_version(ns.package, serial=ns.serial)))
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')

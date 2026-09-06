"""
Файлы на устройстве: положить, снять, посмотреть каталог.

Ручная работа сессий — `adb push`, `adb pull`, `adb shell ls -t` с
разбором вывода на коленке. Здесь то же короче: `latest` отвечает на
вопрос «какий файл свежий» (скачался ли PDF и как его зовут — имя
генерируется на устройстве), а `ls` отдаёт готовые строки со значением
размера и времени.

Время `ls` — только с точностью до минуты, это ограничение toybox; когда
нужны секунды (отличить скачивание от старого файла того же имени),
снимок берут `stat` — как в `adb_doc`.
"""
import argparse
import fnmatch
import json
import os
import re

from adb_.adb_ import adb_run

ADB_FILE_TIMEOUT = 60.0

# toybox `ls -l`: права, число ссылок, владелец, группа, размер, дата
# [время], имя — до конца строки (в именах бывают пробелы).
_ADB_FILE_LS_LINE = re.compile(
    r"^(\S)\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?\s+(.+)$")


def adb_file_push(local_path: str, remote_path: str, serial: str = '',
                  timeout: float = ADB_FILE_TIMEOUT) -> str:
    """
    Положить файл с машины на устройство.

    Args:
        local_path: файл на хосте.
        remote_path: полный путь на устройстве (каталог должен существовать).
        serial: устройство; пусто — единственное подключённое.
        timeout: секунды на передачу.

    Returns:
        `remote_path` — чтобы врезать вызов в цепочку.

    Raises:
        ValueError: локального файла нет.
        RuntimeError: adb подвёл.
    """
    if not os.path.exists(local_path):
        raise ValueError(f"файла {local_path} на машине нет")
    adb_run('push', local_path, remote_path, serial=serial, timeout=timeout)
    return remote_path


def adb_file_pull(remote_path: str, local_dir: str = '.', serial: str = '',
                  timeout: float = ADB_FILE_TIMEOUT) -> str:
    """
    Снять файл с устройства в каталог на машине.

    Args:
        remote_path: полный путь на устройстве.
        local_dir: каталог на хосте, создаётся, если его нет.
        serial: устройство; пусто — единственное подключённое.
        timeout: секунды на передачу.

    Returns:
        Путь скачанного файла на хосте.

    Raises:
        RuntimeError: adb подвёл или файл на хосте не появился.
    """
    os.makedirs(local_dir, exist_ok=True)
    target = os.path.join(local_dir, os.path.basename(remote_path))
    adb_run('pull', remote_path, local_dir, serial=serial, timeout=timeout)
    if not os.path.exists(target):
        raise RuntimeError(f"adb pull {remote_path}: {target} не появился")
    return target


def adb_file_ls(remote_dir: str, pattern: str = '', serial: str = '') -> list:
    """
    Список каталога на устройстве.

    Args:
        remote_dir: каталог на устройстве.
        pattern: фильтр по имени в стиле shell (`*.pdf`); пусто — все.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        `[{'name','size','mtime','dir'}]`; `mtime` — строка
        `ГГГГ-ММ-ДД ЧЧ:ММ` (или одна дата у старых файлов) — минуты, не
        секунды, это потолок toybox `ls`.

    Raises:
        RuntimeError: каталога на устройстве нет.
    """
    out = adb_run('shell', f'ls -l {remote_dir}', serial=serial)
    if 'No such file' in out or 'access' in out and 'ls:' in out:
        raise RuntimeError(f"каталога {remote_dir} на устройстве нет")
    rows = [r for r in (_ls_line(line) for line in out.splitlines()) if r]
    if pattern:
        rows = [r for r in rows if fnmatch.fnmatch(r['name'], pattern)]
    return rows


def adb_file_latest(remote_dir: str, pattern: str = '*', serial: str = '') -> dict:
    """
    Самый свежий файл в каталоге — ответ на «скачалось ли и как зовётся».

    Args:
        remote_dir: каталог на устройстве.
        pattern: фильтр по имени (`*.pdf`); по умолчанию любой файл.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        Строка из `adb_file_ls` самого нового файла.

    Raises:
        RuntimeError: каталога нет или в нём нет файлов под фильтр.
    """
    files = [r for r in adb_file_ls(remote_dir, pattern, serial=serial) if not r['dir']]
    if not files:
        raise RuntimeError(f"в {remote_dir} нет файлов под {pattern!r}")
    return max(files, key=lambda r: r['mtime'])


def _ls_line(line: str) -> dict:
    """Строка toybox `ls -l` в словарь; `total` и прочий мусор — None."""
    match = _ADB_FILE_LS_LINE.match(line.rstrip('\r'))
    if not match:
        return None
    kind, size, date, clock, name = match.groups()
    return {"name": name, "size": int(size),
            "mtime": f"{date} {clock}" if clock else date,
            "dir": kind == 'd'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Файлы на андроид-устройстве.')
    parser.add_argument('command', choices=['push', 'pull', 'ls', 'latest'])
    parser.add_argument('src', help='push: файл на машине; pull/ls/latest: путь на устройстве')
    parser.add_argument('dst', nargs='?', default='', help='push: путь на устройстве; pull: каталог на машине')
    parser.add_argument('--pattern', default='', help='фильтр имени для ls/latest')
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    ns = parser.parse_args()
    try:
        if ns.command == 'push':
            print(adb_file_push(ns.src, ns.dst, serial=ns.serial))
        elif ns.command == 'pull':
            print(adb_file_pull(ns.src, ns.dst or '.', serial=ns.serial))
        elif ns.command == 'ls':
            print(json.dumps(adb_file_ls(ns.src, ns.pattern, serial=ns.serial),
                             ensure_ascii=False, indent=1))
        else:
            print(json.dumps(adb_file_latest(ns.src, ns.pattern, serial=ns.serial),
                             ensure_ascii=False))
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f"ошибка: {err}")

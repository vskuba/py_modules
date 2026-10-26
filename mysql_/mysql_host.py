"""
Куда подключаться к базе: изнутри сети docker и снаружи адрес разный.

`.env` описывает базу так, как её видит приложение: `MYSQL_HOST` — имя сервиса
compose, `MYSQL_PORT` — порт внутри сети. С машины разработчика это имя не
резолвится, а наружу тот же порт опубликован другим номером. Отсюда и берётся
привычка ходить в базу через `docker exec … mysql -u… -p…`: пароль в командной
строке (виден в `ps`, оседает в истории оболочки), результат — рисунок псевдографикой,
разобрать который машинно нельзя.

Модуль отвечает на один вопрос — «какой адрес рабочий отсюда»: сначала пробует
адрес из `.env` как есть (так базу видит приложение в контейнере), затем
спрашивает docker, каким портом наружу опубликован тот же контейнер.

Контейнер ищется **по каталогу проекта**, а не по имени: compose держит его в
метке `com.docker.compose.project.working_dir`. Имена у проектов свои, а баз на
машине несколько — угадывание по имени приводит в чужую. Каталог берётся у
основной выкладки (`project_main_root`): compose поднимали в ней, и метка хранит
её путь, поэтому из `worktree` поиск тоже находит контейнер, а `docker compose`
в том же каталоге — уже нет («service is not running»).

Аргумент `root` тех же функций нацеливает на **соседний checkout**: адрес, порт
и учётка берутся из его `.env`, контейнер ищется по его каталогу. Без него
привычка «заглянуть в базу соседа» остаётся привычкой `docker exec <сосед>
mysql -u… -p…` с тем же паролем в `ps`.
"""
import re
import socket
import subprocess

from config.config import config_setting
from project_.project_ import project_main_root

# Метка compose с каталогом, из которого проект подняли. По ней контейнер и ищется.
MYSQL_HOST_LABEL_DIR = 'com.docker.compose.project.working_dir'
MYSQL_HOST_LABEL_SERVICE = 'com.docker.compose.service'


def mysql_host_address(timeout: float = 2.0, root: str = '') -> tuple:
    """
    Рабочий адрес базы для процесса, который выполняется прямо сейчас.

    Args:
        timeout: секунды на проверку связи по адресу из `.env`.
        root: путь к **соседнему** checkout: адрес и порт берутся из его `.env`,
            контейнер ищется по его каталогу. Пусто — этот проект.

    Returns:
        Пара `(хост, порт)`: либо из `.env` как есть, либо петля и порт,
        опубликованный контейнером наружу.

    Raises:
        RuntimeError: адрес из `.env` не отвечает и контейнер не найден.
    """
    host = str(config_setting('MYSQL_HOST', 'mysql', root))
    port = int(config_setting('MYSQL_PORT', '3306', root) or 3306)

    if _reachable(host, port, timeout):
        return host, port

    published = _published_port(host, port, root)
    if published:
        return '127.0.0.1', published

    where = f"выкладки '{root}'" if root else f'проекта ({project_main_root()})'
    raise RuntimeError(
        f"База недоступна: '{host}:{port}' из .env не отвечает, и контейнер {where} "
        f"не найден. Поднят ли compose?")


def mysql_host_container(root: str = '') -> str:
    """
    Имя контейнера с базой — для того, что делается только внутри него (дамп, консоль).

    Args:
        root: путь к соседнему checkout; пусто — этот проект.

    Returns:
        Имя контейнера; пусто — контейнер не найден или docker недоступен.
    """
    found = _container_find(str(config_setting('MYSQL_HOST', 'mysql', root)),
                            int(config_setting('MYSQL_PORT', '3306', root) or 3306), root)
    return found[0] if found else ''


# ── детали реализации ──

def _reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _published_port(service: str, container_port: int, root: str = '') -> int:
    found = _container_find(service, container_port, root)
    return found[1] if found else 0


def _container_find(service: str, container_port: int, root: str = '') -> tuple:
    """
    Контейнер проекта, публикующий нужный порт: `(имя, порт наружу)` или пусто.

    Сначала ищется сервис с тем же именем, что `MYSQL_HOST` (обычный случай), затем —
    любой контейнер проекта, публикующий этот порт: сервис бывает назван иначе.
    """
    rows = _docker_ps(root)
    named = [row for row in rows if row[1] == service]

    for row in (*named, *rows):
        published = _port_parse(row[2], container_port)
        if published:
            return row[0], published
    return ()


def _docker_ps(root: str = '') -> list:
    """Контейнеры проекта: список `(имя, сервис, порты)`; docker недоступен — пусто."""
    where = str(project_main_root(root))
    command = ['docker', 'ps',
               '--filter', f'label={MYSQL_HOST_LABEL_DIR}={where}',
               '--format', '{{.Names}}\t{{.Label "' + MYSQL_HOST_LABEL_SERVICE + '"}}\t{{.Ports}}']
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []

    rows = []
    for line in done.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _port_parse(ports: str, container_port: int) -> int:
    """Порт наружу из строки вида `33060/tcp, 0.0.0.0:3302->3306/tcp`; не опубликован — 0."""
    for published, inside in re.findall(r'[^,]*?:(\d+)->(\d+)/tcp', ports):
        if int(inside) == container_port:
            return int(published)
    return 0

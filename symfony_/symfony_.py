"""
Console-команды Symfony-проекта (php-fpm в docker-compose) из Python: запуск, php84-разовый запуск.

«bin/console из хост-шелла не годен: конфиг-кэш контейнера собирается при `compose
up`, а разовый `docker run` без memory_limit/xdebug дает `Allowed memory
exhausted` или xdebug.start_with_request» — вход: тот же контейнер php-fpm и та
же сборка, а не хостный php; v2 `docker compose` предпочтителен — v1
`docker-compose` ломает пересоздание контейнера на `KeyError: 'ContainerConfig'`.

Модуль не знает про конкретный проект: корень ищет по `bin/console`, контейнер
и путь console-бинаря берутся из аргументов (дефолты — payment-gateway-стек).
"""
import os
import re
import shutil
import subprocess

# ── константы ──

SYMFONY_CONSOLE_BIN = '/usr/src/app/bin/console'  # путь console-бинаря ВНУТРИ контейнера
SYMFONY_PHP_CONTAINER = 'php-fpm'  # имя сервиса php-fpm в docker-compose проекта
SYMFONY_PHP84_INI = ('memory_limit=2048M', 'error_reporting=2437', 'xdebug.mode=debug',
                     'xdebug.start_with_request=yes')  # «php84: memory_limit=2048M, error_reporting=2437, xdebug.mode=debug»

# ── публичный API модуля ──


def symfony_project_root(cwd: str = None, max_up: int = 8) -> tuple:
    """Сыскать корень Symfony-проекта: каталог с `bin/console`, поднимаясь вверх.

    Args:
        cwd: каталог старта (os.getcwd() по умочалению).
        max_up: сколько каталогов вверх облазить.

    Returns:
        (путь, None) либо (None, 'текст ошибки').
    """
    root = os.path.abspath(cwd or os.getcwd())
    for _ in range(max_up):
        if os.path.isfile(os.path.join(root, 'bin', 'console')):
            return root, None
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    return None, f'корень Symfony не сыскан (up на {max_up}): в {cwd or os.getcwd()} нет bin/console'


def symfony_console_run(args: str, cwd: str = None, timeout: int = 180,
                        container: str = SYMFONY_PHP_CONTAINER,
                        console_bin: str = SYMFONY_CONSOLE_BIN, env: dict = None) -> tuple:
    """Выполнить console-команду в контейнере php-fpm (compose exec, v2 вприоритете).

    «php-fpm из docker-compose exec: конфиг-кэш контейнера собирается при `up`,
    а разовый `docker run` без memory_limit/xdebug.mode=debug дает fatal при
    первом же `$this->getDoctrine()`» — потому exec живого контейнера, а не run.

    Args:
        args: строка аргументов после `bin/console` (`-e=test` и опции — сюда).
        cwd: каталог проекта (по умочалению — сыскать).
        timeout: таймаут процесса, сек.
        container: имя сервиса php-fpm в docker-compose проекта.
        console_bin: путь console-бинаря внутри контейнера.
        env: окружение дочернему процессу (os.environ по умочалению).

    Returns:
        (rc, stdout, err) — err None при rc==0; err — текст, когда команды нет.
    """
    root, err = symfony_project_root(cwd)
    if err:
        return None, None, err
    compose = 'docker compose' if shutil.which('docker') and _symfony_compose_v2() else 'docker-compose'
    argv = f'{compose} exec -T {container} php {console_bin} {args}'
    proc = subprocess.run(argv, shell=True, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        return proc.returncode, proc.stdout, f'console вернул {proc.returncode}: ' \
                                            f'{(proc.stderr or proc.stdout).strip()[:300]}'
    return proc.returncode, proc.stdout, None


def symfony_console_php84_run(args: str, cwd: str = None, timeout: int = 300,
                              ini: tuple = SYMFONY_PHP84_INI, memory_limit: str = '2048M',
                              image: str = '', env: dict = None) -> tuple:
    """Разовый запуск console вне контейнера: `docker run --rm -i` с php84-ini (прокид).

    Когда контейнер не поднят (свежий чек-аут, CI) — `docker compose up` дорог, а
    `docker run --rm -i php -d memory_limit=2048M,... bin/console` дает тот же
    php84+xdebug без поднятия стека.

    Args:
        args: строка аргументов после `bin/console`.
        cwd: каталог проекта (по умочалению — сыскать).
        timeout: таймаут процесса, сек.
        ini: php-ини-флаги (`-d` на каждый).
        memory_limit: `memory_limit=` для `-d` (0 — без лимита).
        image: образ php; пусто — брать `image:` из docker-compose.yml.
        env: окружение дочернему процессу.

    Returns:
        (rc, stdout, err) — err None при rc==0.
    """
    root, err = symfony_project_root(cwd)
    if err:
        return None, None, err
    if not image:
        image, err = _symfony_php_image(root)
        if err:
            return None, None, err
    ini_argv = ' '.join(f'-d {flag}' for flag in (memory_limit and
                                                 (memory_limit, ) + ini or ini))
    argv = f'cd {root} && docker run --rm -i {image} php {ini_argv} {SYMFONY_CONSOLE_BIN} {args}'
    proc = subprocess.run(argv, shell=True, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        return proc.returncode, proc.stdout, f'console (разовый) вернул {proc.returncode}: ' \
                                             f'{(proc.stderr or proc.stdout).strip()[:300]}'
    return proc.returncode, proc.stdout, None


def symfony_compose_run(argv: str, cwd: str = None, timeout: int = 180, env: dict = None) -> tuple:
    """Выполнить произволную команду внутри сервиса php-fpm (`compose run`/`exec`).

    Args:
        argv: строка аргументов после `exec -T php-fpm`.
        cwd: каталог проекта.
        timeout: таймаут процесса, сек.
        env: окружение дочернему процессу.

    Returns:
        (rc, stdout, err) — err None при rc==0.
    """
    root, err = symfony_project_root(cwd)
    if err:
        return None, None, err
    compose = 'docker compose' if shutil.which('docker') and _symfony_compose_v2() else 'docker-compose'
    proc = subprocess.run(f'cd {root} && {compose} exec -T {SYMFONY_PHP_CONTAINER} {argv}',
                          shell=True, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        return proc.returncode, proc.stdout, f'{argv.split()[0]} вернул {proc.returncode}: ' \
                                            f'{(proc.stderr or proc.stdout).strip()[:300]}'
    return proc.returncode, proc.stdout, None



# ── детали реализации ──


def _symfony_compose_v2() -> bool:
    """Есть ли v2-плагин `docker compose` (v1 ломает пересоздание на ContainerConfig)."""
    return subprocess.run('docker compose version >/dev/null 2>&1', shell=True).returncode == 0


def _symfony_php_image(root: str) -> tuple:
    """Сыскать образ php из docker-compose.yml проекта (image: у сервиса php-fpm)."""
    for compose_file in ('docker-compose.yml', 'docker-compose.yaml', 'docker-compose.override.yml'):
        path = os.path.join(root, compose_file)
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, 'r', encoding='utf-8').read()
        except OSError as err:
            return None, f'docker-compose.yml не читается ({path}): {err}'
        block = text.replace('\r\n', '\n')
        m = re.search(rf'^\s*{SYMFONY_PHP_CONTAINER}\s*:\s*$(?:\n\s+#.*$)*\n\s+image:\s*(\S+)',
                       block, re.M)
        if m:
            return m.group(1), None
    return None, f'образ php не сыскан в docker-compose.yml ({root}): добавь image у {SYMFONY_PHP_CONTAINER}'


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('root', 'run'), help='что показать')
    _P.add_argument('--args', default='', help='аргументы console (после bin/console)')
    _P.add_argument('--cwd', default=None, help='каталог проекта')
    _C = _P.parse_args()
    if _C.cmd == 'root':
        _root, _err = symfony_project_root(_C.cwd)
        if _err:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        print(_root)
    else:
        _rc, _out, _err = symfony_console_run(_C.args, _C.cwd)
        if _err:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        print(_out, end='')
    raise SystemExit(0)
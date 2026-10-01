"""
Панель задачи над worktree: поднять, дождаться здоровья, снять — и ссылки целы.

Выкладка задачи временная: контейнер монтирует её каталог вместо репозитория,
и пока в каталоге нет ссылок на хостовые пути (`.env`, `.venv`, общий слой),
контейнер видит пустой `/app` и падает на `Could not import module`, что про
исчезнувший worktree не говорит ничего. Ссылки и переменную каталога подставляли
руками, сеть compose создавали новую — и ловили «all predefined address pools
have been fully subnetted» на машине, где мостовых сетей уже под тридцать.

Здесь круг целиком: проверить ссылки, поднять сервис от корня **основной**
выкладки (compose помнит проект от своего каталога — сеть берётся готовая),
передать переменной путь каталога задачи, дождаться `/health`, вернуть base url
для сюит. На выходе — снять сервис, пока каталог не удалён.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys

from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from http_.http_pool import http_pool_transport
from project_.project_ import project_git

# Что обязано быть ссылкой в каталоге задачи: compose монтирует каталог задачи
# целиком, а симлинки из основной выкладки в него не едут — без них `.env`
# пустой, интерпретатор не тот, общий слой не виден.
PROJECT_WORKTREE_LINKS = ('.env', '.env.local', '.venv', 'py_modules')

# Имя переменной, из которой compose берёт каталог задачи. Другой проект зовёт
# своей — поэтому именем можно поделиться, а не держать зашитым.
PROJECT_WORKTREE_DIR_VAR = 'PROJECT_TASK_WORKTREE_DIR'


async def project_worktree_up(worktree: str = '', service: str = 'uvicorn_task',
                              dir_var: str = PROJECT_WORKTREE_DIR_VAR,
                              links: tuple = PROJECT_WORKTREE_LINKS,
                              timeout: float = 120) -> str:
    """Поднять сервер задачи над каталогом задачи и вернуть его base url.

    Сервис ищется в compose-проекте **основной** выкладки (её корень — общий
    `.git` каталога задачи, не место модуля; оттого сеть берётся готовая, а не
    заводится новая), поднимается с каталогом задачи в `dir_var`, ждёт `/health`
    и отдаёт адрес со своего опубликованного порта — тем же путём сюиты берут
    `BASE_URL`.

    Args:
        worktree: каталог задачи; пусто — текущий (обычно его и снимают).
        service: подстрока имени compose-сервиса задачи.
        dir_var: имя переменной окружения, из которой compose берёт каталог.
        links: чего не хватает в каталоге задачи из корня основной выкладки.
        timeout: секунд на подъём и здоровье.

    Returns:
        base url поднятой панели задачи — для `BASE_URL` сюит.

    ⚠⚠ Каталог задачи временный: сервис переживает свой каталог и при следующем
    старте монтирует точку заново. Сняли каталог — снимайте сервис
    `project_worktree_down()`, а не бросайте контейнер над пустым `/app`.
    """
    wt = Path(worktree or Path.cwd()).resolve()
    root = _main_of(wt)
    _ensure_links(wt, root, links)

    env = {**os.environ, dir_var: str(wt)}
    row = _found(_ps(root), service)
    name = _name(row) if row else service
    cmd = ['restart', name] if row else ['up', '-d', service]
    subprocess.run(['docker', 'compose', *cmd], cwd=str(root), env=env,
                   check=True, capture_output=True, text=True, timeout=timeout)

    return await _alive(_base(_found(_ps(root), name)), timeout=timeout)


def project_worktree_down(service: str = 'uvicorn_task') -> str:
    """Снять сервис задачи: контейнер больше не висит над удалённым каталогом."""
    root = _main_of(Path.cwd())
    row = _found(_ps(root), service)
    name = _name(row) if row else service
    for cmd in (['stop', name], ['rm', '-f', name]):
        subprocess.run(['docker', 'compose', *cmd], cwd=str(root),
                       check=True, capture_output=True, text=True, timeout=60)

    return f'сервис {name} снят'


# ── детали реализации ──

def _main_of(checkout: Path) -> Path:
    """Корень основной выкладки для этого checkout — родитель общего `.git`.

    ⚠ Не `project_main_root()`: тот берёт корень от места **модуля**, а здесь
    нужен корень **переданной выкладки** — git отвечает на `--git-common-dir` из
    самой копии, откуда бы ни был загружен этот модуль.
    """
    common = project_git(checkout, 'rev-parse', '--git-common-dir')
    if not common:
        return checkout
    path = Path(common)
    if not path.is_absolute():
        path = (checkout / path).resolve()
    return path.parent if path.name == '.git' else checkout


def _ensure_links(worktree: Path, root: Path, links: tuple) -> None:
    """Чего нет в каталоге задачи из основной выкладки — то её симлинк на хост."""
    for one in links:
        here, there = worktree / one, root / one
        if here.is_symlink() or here.exists() or not there.exists():
            continue
        here.symlink_to(there)


def _ps(root: Path) -> list:
    """Строки `docker compose ps --format json` из корня основной выкладки."""
    done = subprocess.run(['docker', 'compose', 'ps', '--format', 'json'],
                          cwd=str(root), capture_output=True, text=True,
                          timeout=20)
    rows = []
    for line in (done.stdout or '').splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _found(rows: list, service: str) -> dict:
    """Поднятая строка ps по подстроке имени сервиса; пусто — сервис не поднят."""
    for row in rows:
        if service in _name(row):
            return row
    return {}


def _name(row: dict) -> str:
    """Имя сервиса из строки ps: `Service`, а в старых форматах — из `Names`."""
    if row.get('Service'):
        return row['Service']
    return (row.get('Names') or [''])[0].lstrip('/')


def _base(row: dict) -> str:
    """http-адрес панели из её строки ps: первый опубликованный порт.

    ⚠ `Ports` в этом формате — строка `'0.0.0.0:8013->8000/tcp, ...'`, а не
    список: обходить её по буквам — не найти в букве `->` и потерять порт.
    """
    ports = row.get('Ports') or ''
    for part in (ports if isinstance(ports, str) else ','.join(ports)).split(','):
        if '->' in part:
            return f'http://127.0.0.1:{part.split("->")[0].rsplit(":", 1)[-1]}'
    raise ValueError(f'панель {_name(row)!r} без опубликованного порта в ps')


async def _alive(base: str, timeout: float) -> str:
    """Ждать ответа `/health`: любой ответ — признак живого приложения.

    ⚠ Панель задачи под сторожем и на `/health` отвечает **401**: ждать 2xx,
    как `uvicorn_dev_health`, — не дождаться никогда. Здесь же connection
    refused — настоящий мёртвый знак, а любой HTTP-ответ — живой.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last = ''
    while True:
        try:
            async with httpx.AsyncClient(transport=http_pool_transport(),
                                         timeout=3) as c:
                await c.get(f'{base}/health')
            return base                      # любой HTTP-ответ — панель жива
        except Exception as err:
            last = str(err) or type(err).__name__
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f'панель {base} не подала признаков жизни за '
                               f'{timeout:g} с: {last}')
        await asyncio.sleep(0.5)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Панель задачи над worktree: поднять и дождаться '
                    'здоровья, снять.')
    ap.add_argument('command', choices=['up', 'down'],
                    help='up — поднять над каталогом и вернуть base url; '
                         'down — снять сервис')
    ap.add_argument('--worktree', default='', help='каталог задачи; пусто — '
                    'текущий')
    ap.add_argument('--service', default='uvicorn_task',
                    help='подстрока имени compose-сервиса')
    ap.add_argument('--timeout', type=float, default=120, help='секунды')
    ns = ap.parse_args()
    try:
        if ns.command == 'up':
            print(asyncio.run(project_worktree_up(
                ns.worktree, ns.service, timeout=ns.timeout)))
        else:
            print(project_worktree_down(ns.service))
    except (RuntimeError, TimeoutError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

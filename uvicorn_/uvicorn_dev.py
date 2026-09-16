"""
Dev-цикл панели: перезапустить, дождаться здоровья, не гадать вслепую.

Правка шаблона или Python'а живёт в контейнере, а не в голове агента: панель
перезапускают (`docker compose restart <сервис>` из корня **основной выкладки**
— compose помнит проект от каталога, из которого его поднимали), и ждут, когда
она снова ответит. `sleep 3` угадывает не всегда: старт с миграциями дольше —
агент решает, что панель упала; короче — и первый запрос бьётся в ещё не
поднявшийся uvicorn с невнятным connection refused.

Здесь цикл: `await uvicorn_dev_up()` — поднят, здоровье проверено, base url в
руках; дальше за дело идёт `uvicorn_panel_client` (он и про перелогин после
рестарта). Готовность — не таймер, а `/health`: пока панель не ответила,
ничего не проверено.

⚠ Шаблоны Jinja кэшируются в процессе панели: правку на диске контейнер
увидит только после рестарта — этот модуль рестарт и есть, а не `--reload`
uvicorn (воркеру своя смена, и reload ронял бы круги смены на каждой букве).
"""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from http_.http_pool import http_pool_transport


async def uvicorn_dev_health(base: str = '', timeout: float = 30) -> str:
    """Ждать `/health` поднятой панели; вернуть её base url.

    Returns:
        base url — заодно и адрес, если звали без него (`uvicorn_panel_base`).

    Raises:
        TimeoutError: за `timeout` панель не ответила 2xx — старт не завершился.
    """
    from uvicorn_.uvicorn_panel import uvicorn_panel_base
    base = (base or await uvicorn_panel_base()).rstrip('/')
    deadline = asyncio.get_running_loop().time() + timeout
    last = ''
    while True:
        try:
            async with httpx.AsyncClient(transport=http_pool_transport(),
                                         timeout=3) as c:
                r = await c.get(f'{base}/health')
            if r.is_success:
                return base
            last = f'HTTP {r.status_code}'
        except Exception as err:
            last = str(err) or type(err).__name__
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f'панель {base} не подала признаков здоровья за '
                               f'{timeout:g} с: {last}')
        await asyncio.sleep(0.5)


async def uvicorn_dev_up(service: str = 'uvicorn', restart: bool = True,
                         timeout: float = 30) -> str:
    """Поднять сервис смены (restart или up, если стоит) и дождаться здоровья.

    Args:
        service: подстрока имени compose-сервиса (по умолчанию — uvicorn-сервис).
        restart: False — только up (ещё не поднят), True — рестарт живого.

    Returns:
        base url поднятой панели — для `uvicorn_panel_client`.

    Raises:
        RuntimeError: compose не нашёл сервиса; TimeoutError — не дождался.
    """
    from project_.project_ import project_main_root
    from uvicorn_.uvicorn_panel import uvicorn_panel_base
    root = project_main_root()
    done = subprocess.run(['docker', 'compose', 'ps', '--format', 'json'],
                          cwd=str(root), capture_output=True, text=True,
                          timeout=20)
    name = ''
    for line in (done.stdout or '').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        candidate = row.get('Service') or (row.get('Names') or [''])[0]
        if service in candidate:
            name = candidate
            break
    if not name:
        raise RuntimeError(f'в compose-проекте {root.name} нет сервиса с {service!r} '
                           f'в имени — поднят ли контейнер?')
    cmd = ['restart', name] if restart else ['up', '-d', name]
    subprocess.run(['docker', 'compose', *cmd], cwd=str(root), check=True,
                   capture_output=True, text=True, timeout=timeout)
    return await uvicorn_dev_health(timeout=timeout)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Цикл разработки панели: дождаться здоровья, рестартануть и '
                    'снова дождаться.')
    ap.add_argument('command', choices=['health', 'up'],
                    help='health — просто ждать; up — рестарт и ждать')
    ap.add_argument('--service', default='uvicorn', help='подстрока имени сервиса')
    ap.add_argument('--base', default='', help='адрес панели; пусто — сам')
    ap.add_argument('--timeout', type=float, default=30, help='секунды')
    ns = ap.parse_args()
    try:
        if ns.command == 'health':
            print(asyncio.run(uvicorn_dev_health(ns.base, ns.timeout)))
        else:
            print(asyncio.run(uvicorn_dev_up(ns.service, timeout=ns.timeout)))
    except (RuntimeError, TimeoutError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

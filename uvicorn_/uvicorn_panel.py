"""
Клиент запущенной панели для проверок живьём: вошёл и_workает, без heredoc'ов.

Агент проверяет панель HTTP-запросом десять раз за сессию, и каждый раз руками
пишет одно и то же: войти (`/auth/login` **JSON**-телом — на форме-encode он
отвечает 422), поймать куки, не перепутать `localhost` и `127.0.0.1` (кука с
чужого домена до запроса просто не доезжает — молчаливая 401), перелогиниться
после рестарта (куки умирают вместе с процессом). Ошибка в любом из этих шагов
выглядит как «панель сломалась», а не как «я забыл куку».

Здесь это один вызов: `await uvicorn_panel_client()` — и в руках готовый к запросу
клиент, который сам войдёт под учёткой из `.env` и сам перелогинится на 401.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from config.config import config_get
from http_.http_pool import http_pool_transport


class UvicornPanelClient(httpx.AsyncClient):
    """Клиент запущенной панели: протухшая кука чинит сам, перезаходом."""

    _credentials: tuple[str, str] = ('', '')

    async def request(self, method, url, **kw):        # type: ignore[override]
        response = await super().request(method, url, **kw)
        if response.status_code == 401 and self._credentials[0]:
            self.cookies = await _login(str(self.base_url), *self._credentials)
            response = await super().request(method, url, **kw)
        return response


async def uvicorn_panel_base(service: str = '', port: str = '') -> str:
    """Адрес панели снаружи: `BASE_URL`, а иначе — опубликованный порт compose.

    Args:
        service: имя compose-сервиса; пусто — первый поднявшийся с портом.
        port: внешний порт выбирать по нему; пусто — первый опубликованный.

    Returns:
        http://127.0.0.1:<порт> — всегда адрес, с которого куку видно запросу.
    """
    from project_.project_ import project_main_root
    import subprocess

    explicit = config_get('BASE_URL')
    if explicit:
        return explicit.rstrip('/')

    done = subprocess.run(['docker', 'compose', 'ps', '--format', 'json'],
                          cwd=str(project_main_root()), capture_output=True,
                          text=True, timeout=20)
    rows = []
    for line in (done.stdout or '').splitlines():          # JSONL: строка — сервис
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    for row in rows:
        name = row.get('Service') or (row.get('Names') or [''])[0]
        if row.get('State') not in ('', 'running') or \
                (service in name if service else 'uvicorn' not in name):
            continue
        for pub in row.get('Publishers', []):
            host_port = str(pub.get('PublishedPort') or '')
            if not host_port or host_port == '0' or (port and host_port != port):
                continue
            return f'http://127.0.0.1:{host_port}'
    raise ValueError(f'панель не видна в compose ps (сервис {service!r}, '
                     f'порт {port!r}) — поднят ли контейнер?')


async def uvicorn_panel_client(base: str = '', user: str = '',
                               password: str = '') -> UvicornPanelClient:
    """Вошедший клиент панели: куки правильные, 401 — сам перелогин.

    Args:
        base: адрес; пусто — `uvicorn_panel_base()`.
        user, password: учётка; пусто — ADMIN_USERNAME/ADMIN_PASSWORD из `.env`.

    Returns:
        `UvicornPanelClient` (httpx.AsyncClient): .get/.post/.put/.delete —
        готовыми к запросу, с авторетраем после перезахода.
    """
    base = (base or await uvicorn_panel_base()).rstrip('/')
    user = user or config_get('ADMIN_USERNAME', 'admin')
    password = password or config_get('ADMIN_PASSWORD', 'change-me')
    client = UvicornPanelClient(base_url=base, timeout=15,
                                transport=http_pool_transport())
    client.cookies = await _login(base, user, password)
    client._credentials = (user, password)
    return client


# ── детали реализации ──

async def _login(base: str, user: str, password: str) -> httpx.Cookies:
    """Вход контрактом панели: JSON-тело, не форма (форма — 422)."""
    async with httpx.AsyncClient(transport=http_pool_transport(), timeout=15) as c:
        response = await c.post(f'{base}/auth/login',
                                json={'username': user, 'password': password})
    if response.status_code not in (200, 201, 204):
        raise RuntimeError(f'вход в {base} не удался: {response.status_code} '
                           f'{response.text[:200]}')
    return response.cookies


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Запрос к запущенной панели под учёткой из .env: '
                    'метод, путь — тело как есть. Клиент с общим транспортом '
                    'не закрывают (http_pool) — оттого без async with.')
    ap.add_argument('method', nargs='?', default='GET',
                    choices=['GET', 'POST', 'PUT', 'DELETE'])
    ap.add_argument('path', default='/', help='путь от корня панели')
    ap.add_argument('--body', default='', help='JSON-тело запроса')
    ap.add_argument('--base', default='', help='адрес панели; пусто — сам')
    ns = ap.parse_args()
    try:
        async def _go():
            client = await uvicorn_panel_client(base=ns.base)
            r = await client.request(ns.method, ns.path,
                                     content=ns.body or None,
                                     headers={'Content-Type': 'application/json'}
                                     if ns.body else None)
            print(f'{r.status_code} {len(r.content)} байт')
            print(r.text[:4000])
        asyncio.run(_go())
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

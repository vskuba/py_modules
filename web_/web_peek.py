"""
Сырой ответ живой страницы без браузера: статус, заголовки, тело, маркеры.

Когда нужен факт «что сервер реально прислал», а не что нарисовал Chrome:
доехал ли шаблон, в каком виде уходит JSON, под каким заголовком стоит кука,
есть ли в странице её кусок. headless-Chrome-зонды (`web_probe`) для этого
тяжеловаты: они считают геометрию там, где нужен один `grep` по ответу.

Тело возвращается как есть и, если это JSON, разобранным тоже — потому что
двери панелей отвечают JSON'ом, и проверять поле поля дешевле, чем искать его
подстрокой. Маркеры ищутся построчно и называют номер строки: «куски шаблона
видны на таких-то строках ответа» — тот же grep, что человек сделал бы руками.
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


async def web_peek(url: str, method: str = 'GET', content: str = '',
                   headers: dict | None = None, timeout: float = 15) -> dict:
    """Один запрос без браузера — весь ответ как есть.

    Args:
        url: адрес (полный; относительный — загляните в `uvicorn_panel_base`).
        method: GET/POST/PUT/DELETE.
        content: тело как есть (JSON-строку сервер поймёт по content-type).
        headers: заголовки запроса (свой Content-Type — сюда).
        timeout: секунды.

    Returns:
        {'status': код, 'headers': заголовки словарём, 'length': байт,
         'text': тело текстом, 'json': объект, если это JSON, иначе None}
    """
    async with httpx.AsyncClient(transport=_transport(), timeout=timeout) as c:
        r = await c.request(method, url, content=content or None,
                            headers=headers)
    try:
        parsed = r.json()
    except ValueError:
        parsed = None
    return {'status': r.status_code, 'headers': dict(r.headers),
            'length': len(r.content), 'text': r.text, 'json': parsed}


async def web_peek_markers(url: str, markers: list, **kw) -> dict:
    """Есть ли маркеры в ответе и на какой строке (в духе grep, одним вызовом).

    Args:
        url, kw: см. `web_peek`.
        markers: подстроки, которые ищем построчно в теле ответа.

    Returns:
        {маркер: номер строки или None}; статус ответа — под ключом 'status'.
    """
    page = await web_peek(url, **kw)
    lines = (page['text'] or '').splitlines()
    found = {m: next((i + 1 for i, line in enumerate(lines) if m in line), None)
             for m in markers}
    return {'status': page['status'], **found}


# ── детали реализации ──

def _transport():
    """Общий пул соединений (http_pool); свой клиент с ним не закрывают —
    здесь же клиент живёт ровно один запрос и закрыт вместе с ним."""
    from http_.http_pool import http_pool_transport
    return http_pool_transport()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Сырой ответ живой страницы: статус, тело, маркеры.')
    ap.add_argument('url', help='полный адрес')
    ap.add_argument('--markers', default='', help='подстроки через запятую')
    ap.add_argument('--method', default='GET', help='метод запроса')
    ap.add_argument('--body', default='', help='тело как есть')
    ap.add_argument('--show', action='store_true', help='ещё и тело целиком')
    ap.add_argument('--header', action='append', default=[], metavar='КЛЮЧ=значение',
                    help='заголовок запроса, повторять (ключ API, Cookie, свой Content-Type)')
    ap.add_argument('--timeout', type=float, default=15, help='секунды на запрос')
    ns = ap.parse_args()
    head = dict(h.split('=', 1) for h in ns.header) or None
    try:
        if ns.markers:
            out = asyncio.run(web_peek_markers(
                ns.url, ns.markers.split(','), method=ns.method, content=ns.body,
                headers=head, timeout=ns.timeout))
        else:
            out = asyncio.run(web_peek(ns.url, ns.method, content=ns.body,
                                       headers=head, timeout=ns.timeout))
            if not ns.show:
                out['text'] = out['text'][:500] + ('…' if len(out['text']) > 500
                                                   else '')
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str)[:8000])
    except (httpx.HTTPError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

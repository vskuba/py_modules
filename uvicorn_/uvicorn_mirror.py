"""
Копия страницы панели как полноценный источник: тело — с прокси, всё прочее — на панель.

`uvicorn_page_capture` переписывает корневые ссылки на адрес панели — хорошо для
замера геометрии сохранённого HTML, но страница на копии живёт неполно: её
собственные `fetch` (`${base}/api/...`) уезжают в чужой temp-исток и глохнут без
куки панели. Этот модуль делает из копии настоящий исток: скопированное тело
отдаётся по ТОМУ ЖЕ пути, под которым страница живёт на панели, а всё остальное
(`/static/...`, `/api/...`, любой метод) проксируется на панель вошедшим клиентом
— та же кука, что у `uvicorn_panel_client`.

С тех пор «посмотреть страницу глазами» — одна строка:

    with uvicorn_mirror('/admin/photo/girls') as url:
        web_probe(url, ...)              # или web_shot_capture(url, out=...)

без symlink'ов `static`, sed-ов pathname и слёз про CORS: запросы страницы
same-origin по построению, а `{{ base }}` и относительные ссылки внутри копии
продолжают указывать туда же, куда указывали.

Грабли, из-за которых это не `web_shot_serve` + надежда:
* панель отвечает своей страницей только вошедшему: запрос без куки увидел бы
  login-стену вместо данных — поэтому прокси несёт куку панели, а не изобретает
  вход;
* проксирует ВСЕ методы: страница не только читает — загрузка материалов POST-ом,
  и страница без этого врёт;
* тело копии живёт в памяти и снимается свежим при каждом входе в контекстный
  менеджер: копия из /tmp, пережившая правку шаблона, врёт незаметно.
"""
import argparse
import asyncio
import contextlib
import http.server
import socketserver
import threading

import httpx


def uvicorn_mirror(page: str, base: str = '') -> contextlib.AbstractContextManager:
    """Страница панели как полноценный исток; contextmanager, отдающий URL копии.

    Тело страницы снимается свежим и отдаётся по своему пути; `/static/`, `/api/`
    и всё остальное проксируется на панель вошедшим клиентом.

    Args:
        page: путь страницы на панели ('/admin/photo/girls').
        base: адрес панели; пусто — `uvicorn_panel_base()`.

    Yields:
        str: «http://127.0.0.1:<порт><page>» — этот URL суют `web_probe`,
        `web_shot_capture` и `web_drive_eval` как обычную страницу.
    """
    return _mirror(page, base)


async def _body_and_origin(page: str, base: str) -> tuple[str, str, httpx.Cookies]:
    """Свежая копия страницы и адрес панели с кукой входа — одним заходом."""
    from uvicorn_.uvicorn_panel import uvicorn_panel_client
    client = await uvicorn_panel_client(base=base)
    origin = str(client.base_url).rstrip('/')
    body = (await client.request('GET', page)).text
    cookies = client.cookies
    await client.aclose()
    return body, origin, cookies


@contextlib.contextmanager
def _mirror(page: str, base: str):
    body, origin, cookies = asyncio.run(_body_and_origin(page, base))
    api = httpx.Client(base_url=origin, cookies=cookies, timeout=15,
                      follow_redirects=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        """Путь копии — скопированное тело; любой прочий запрос — панели как есть."""

        def _handle(self):
            if self.path.split('?')[0] == page.split('?')[0]:
                code, data = 200, body.encode()
                kind = 'text/html; charset=utf-8'
            else:
                try:
                    n = int(self.headers.get('content-length', 0) or 0)
                    res = api.request(self.command, self.path,
                                      content=self.rfile.read(n) if n else None)
                except Exception as err:        # панель мертва — честная 502
                    self.send_error(502, f'панель недоступна: {type(err).__name__}')
                    return
                code, data = res.status_code, res.content
                kind = res.headers.get('content-type', 'application/json')
            self.send_response(code)
            self.send_header('content-type', kind)
            self.send_header('content-length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _handle

        def log_message(self, *a):              # шум в журнале проекта не нужен
            pass

    srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    srv.daemon_threads = True
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{srv.server_address[1]}{page.split("?")[0]}'
    finally:
        srv.shutdown()
        srv.server_close()
        api.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Копия страницы панели с проксированным /api: URL для '
                    'web_probe/web_shot/web_drive, будто ты на самой панели.')
    ap.add_argument('page', help='путь страницы на панели, напр. /admin/photo/girls')
    ap.add_argument('--probe', default='', help='тело async-зонда с return; '
                    'пусто — напечатать URL копии и ждать Enter')
    ap.add_argument('--base', default='', help='адрес панели; пусто — сам')
    ns = ap.parse_args()
    with uvicorn_mirror(ns.page, ns.base) as url:
        if ns.probe:
            from web_.web_drive import web_drive_eval
            print(web_drive_eval(url, ns.probe)['value'])
        else:
            print(url)
            input('Enter — закрыть ')

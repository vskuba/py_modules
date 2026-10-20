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
  и страница без этого врёт. ⚠⚠ Вместе с телом едет и `content-type`: без него
  панель получает байты без типа, не берётся разбирать JSON и отвечает **422 на
  исправную ручку**. Ломается это только у пишущих запросов — чтение типа не
  несёт, — поэтому копия при этом выглядит совершенно рабочей;
* тело копии живёт в памяти и снимается свежим при каждом входе в контекстный
  менеджер: копия из /tmp, пережившая правку шаблона, врёт незаметно.
"""
import argparse
import json
import asyncio
import concurrent.futures
import contextlib
import http.server
import socketserver
import threading

import httpx


def uvicorn_mirror(page: str, base: str = '', user: str = '',
                   password: str = '') -> contextlib.AbstractContextManager:
    """Страница панели как полноценный исток; contextmanager, отдающий URL копии.

    Тело страницы снимается свежим и отдаётся по своему пути; `/static/`, `/api/`
    и всё остальное проксируется на панель вошедшим клиентом.

    Args:
        page: путь страницы на панели ('/admin/photo/girls').
        base: адрес панели; пусто — `uvicorn_panel_base()`.
        user, password: под кем входить; пусто — `ADMIN_USERNAME`/
            `ADMIN_PASSWORD` из `.env`.

    Yields:
        str: «http://127.0.0.1:<порт><page>» — этот URL суют `web_probe`,
        `web_shot_capture` и `web_drive_eval` как обычную страницу.

    ⚠ Учётка доводом, а не только из `.env`: `ADMIN_USERNAME` — это личность
    **загрузочного администратора** (`panel_/panel_user.py` заводит его, когда
    таблица пуста), и подменять её служебной учёткой автоматики нельзя. Тому,
    кто ходит сюда своим человеком, довод и нужен.
    """
    return _mirror(page, base, user, password)


async def _body_and_origin(page: str, base: str, user: str = '',
                           password: str = '') -> tuple[str, str, httpx.Cookies]:
    """Свежая копия страницы и адрес панели с кукой входа — одним заходом."""
    from uvicorn_.uvicorn_panel import uvicorn_panel_client
    client = await uvicorn_panel_client(base=base, user=user, password=password)
    origin = str(client.base_url).rstrip('/')
    body = (await client.request('GET', page)).text
    cookies = client.cookies
    await client.aclose()
    return body, origin, cookies


def _run_sync(coro):
    """Дождаться корутины из синхронного кода — и когда цикл уже крутится.

    ⚠ Голый `asyncio.run` здесь падает «cannot be called from a running event
    loop»: копию зовут не только из скрипта, но и из async-кода — кронджоба,
    ручки панели. Цикл чужой, и вложиться в него нельзя; поэтому при живом цикле
    корутина уезжает в отдельный поток со своим циклом. Без этого вызывающему
    пришлось бы самому оборачивать `uvicorn_mirror` в `to_thread`, а он
    contextmanager — обернуть его так не выходит вовсе.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@contextlib.contextmanager
def _mirror(page: str, base: str, user: str = '', password: str = ''):
    body, origin, cookies = _run_sync(_body_and_origin(page, base, user, password))
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
                    # ⚠⚠ `content-type` обязан доехать до панели. Без него тело
                    # уходит байтами без типа, FastAPI не берётся его разбирать
                    # и отвечает 422 — на совершенно исправную ручку. Ломалось
                    # это молча и только у пишущих запросов: чтение (GET) типа
                    # не несёт, и копия выглядела рабочей.
                    kind = self.headers.get('content-type')
                    res = api.request(self.command, self.path,
                                      content=self.rfile.read(n) if n else None,
                                      headers={'content-type': kind} if kind else None)
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
        # ⚠⚠ Адрес отдаётся С query. Тело копии запрашивается у панели вместе с
        # ним (`_body_and_origin`), а браузеру прежде доставался путь без него —
        # и страница, читающая свой адрес (`?workflow_id=5`, `?tab=errors`),
        # открывалась не той, что просили. Ошибки при этом нет ни одной: копия
        # живая, просто показывает состояние по умолчанию.
        #
        # ⚠ Отдавать можно смело: путь копии сверяется обрезанным
        # (`_handle` выше), так что query на выбор «тело или панель» не влияет.
        yield f'http://127.0.0.1:{srv.server_address[1]}{page}'
    finally:
        srv.shutdown()
        srv.server_close()
        api.close()


def _script(body: str, settle: int) -> str:
    """Тело JS к запуску: выдержка до него и sentinel-return после.

    ⚠ Без `return` внутри обёртка `web_drive` ждёт своего потолка молча: код-то
    выполнен, но промис не закрыт — лишний wait_ms на каждый прогон вслепую.
    Тело, которое не обещает значение, дополняется `return 1`, и прогон
    завершается сразу по выполнении.
    """
    head = (f'await new Promise(r => setTimeout(r, {int(settle)}));\n'
            if settle > 0 else '')
    tail = '' if ('return' in body or 'done(' in body) else '\nreturn 1;'
    return head + (body or 'return 1;') + tail


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Копия страницы панели с проксированным /api: URL для '
                    'web_probe/web_shot/web_drive, будто ты на самой панели.')
    ap.add_argument('page', help='путь страницы на панели, напр. /admin/photo/girls')
    ap.add_argument('--probe', default='', help='тело async-зонда с return; '
                    'пусто — напечатать URL копии и ждать Enter')
    ap.add_argument('--base', default='', help='адрес панели; пусто — сам')
    # ⚠ Скрипт стартует на load-событии, а панельный JS дорисовывает содержимое
    # после своих fetch: без выдержки `querySelectorAll` вернёт пустоту, клик по
    # [2] кинет TypeError — и вместо действия получишь таймаут обёртки
    # `web_drive`. `--settle` даёт странице долю секунды договорить.
    ap.add_argument('--settle', type=int, default=1000,
                    help='мс, которые страница договаривает перед скриптом; 0 — сразу')
    # ⚠⚠ Кадр страницы за входом иначе одной командой не снять: `web_shot`
    # по адресу панели получит login-стену, а скрипт на питоне прячет саму
    # работу — по его команде не видно, снимали кадр или нет. И человеку, и
    # заставе вёрстки (`claude_gate_design_review_live`) нужна команда, в
    # которой это написано.
    ap.add_argument('--shot', default='', help='снять кадр страницы в файл')
    ap.add_argument('--size', default='1700x900', help='вьюпорт кадра, ШxВ')
    # ⚠ Страница открывается на вкладке из `localStorage`, а у свежего Chrome
    # он пуст: кадр приходит с умолчательной вкладки, а не с той, которую
    # правили. Этот JS выполняется перед съёмкой — им и переключают.
    ap.add_argument('--js', default='', help='тело async-функции перед кадром')
    ns = ap.parse_args()
    with uvicorn_mirror(ns.page, ns.base) as url:
        if ns.shot:
            from web_.web_drive import web_drive_eval

            width, _, height = ns.size.partition('x')
            got = web_drive_eval(url, _script(ns.js, ns.settle), shot=ns.shot,
                                 size=(int(width), int(height or 900)),
                                 wait_ms=15000)
            bad = [one for one in got.get('console') or []
                   if one['level'] == 'error']
            print(ns.shot)
            # ⚠⚠ Ответ `--js` печатается тоже: его `return` — это числа из
            # DOM, ради которых зонд и писали. Без этого приходится снимать
            # кадр дважды — один раз ради картинки, другой ради чисел.
            if got.get('value') is not None:
                print(json.dumps(got['value'], ensure_ascii=False))
            # ⚠ Ошибки страницы печатаются рядом с кадром: кадр показывает,
            # что человек увидел, а консоль — что при этом сломалось молча.
            for one in bad:
                print('консоль:', one['text'][:160])
        elif ns.probe:
            from web_.web_drive import web_drive_eval
            print(json.dumps(web_drive_eval(url, _script(ns.probe, ns.settle),
                                            wait_ms=10000)['value'],
                             ensure_ascii=False))
        else:
            print(url)
            input('Enter — закрыть ')

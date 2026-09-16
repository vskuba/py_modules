"""
Живая страница панели — целиком, для замеров и сверки раздачи.

`uvicorn_panel` ходит в панель под учёткой, но печатает ответ обрезанным логом:
страница в 30+ КБ из консоли доезжает куском, и зонд с неполным телом
«не дописывает» сам себя. Отсюда две функции:

* `uvicorn_page_capture` — полный HTML живой страницы в файл; корневые ссылки
  (`href="/..."`, `src="/..."`, `url(/...)`) переписаны на адрес панели, иначе
  локальный прогон (`web_probe`, `web_shot`) теряет ассеты и мёртвая разметка
  врёт про геометрию; на выход ставится зонд как на свою страницу;
* `uvicorn_page_serves` — «доехала ли правка в раздачу»: маркер ищется в том,
  что отдаёт панель (страница или статик), ответ — номер строки и сама строка;
  вместо grep по сохранённой копии.

Грабли, из-за которых это не один `client.get`:
* корневые ссылки локального прогона резолвятся не туда: сохранённый HTML
  раздаётся со своего каталога, и `/static/...` уезжает в temp-корень — поэтому
  адреса переписываются на адрес панели;
* ответ печатают с обрезкой — полный HTML в stdout не смотрят, его кладут в
  файл (это и делает capture).
"""
import argparse
import asyncio
import os
import re
import tempfile

from uvicorn_.uvicorn_panel import uvicorn_panel_client

_ATTR_LINK = re.compile(r'\b(href|src|action)="(/[^"]*)"')
_URL_LINK = re.compile(r'url\((["\']?)/(?!["\'])')


async def uvicorn_page_capture(path, out='', method='GET', body=None,
                               base='') -> str:
    """Полный HTML живой страницы — в файл; корневые ссылки переписаны на панель.

    Args:
        path: путь страницы/ресурса на панели ('/admin/photo/search').
        out: куда положить файл; пусто — временный каталог, имя — как у path.
        method, body: запрос (body — dict → JSON).
        base: адрес панели; пусто — `uvicorn_panel_base()`.

    Returns:
        путь к файлу — его можно совать `web_probe`/`web_shot` как страницу.
    """
    client = await uvicorn_panel_client(base=base)
    try:
        resp = await client.request(method, path,
                                    json=body if isinstance(body, (dict, list))
                                    else None)
        resp.raise_for_status()
        origin = str(client.base_url).rstrip('/')
    finally:
        await client.aclose()
    html = _ATTR_LINK.sub(lambda m: f'{m.group(1)}="{origin}{m.group(2)}"',
                         resp.text)
    html = _URL_LINK.sub(lambda m: f'url({m.group(1)}{origin}/', html)
    if not out:
        name = os.path.basename(path.rstrip('/')).split('?')[0] or 'index'
        out = os.path.join(tempfile.mkdtemp(prefix='uvicorn_page_'),
                           f'{name}.html')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(html)
    return out


async def uvicorn_page_serves(marker, path='/', base='') -> dict:
    """Доехала ли правка до раздачи: маркер ищется в ответе панели по path.

    Args:
        marker: что ищем (класс, строка шаблона — что правили).
        path: страница или статик на панели, где правка обязана виднеться.
        base: адрес панели; пусто — сам.

    Returns:
        {'found': bool, 'path': ..., 'line': номер строки или None,
         'text': строка раздачи с маркером (срез, не весь ответ)}.
    """
    client = await uvicorn_panel_client(base=base)
    try:
        resp = await client.request('GET', path)
        resp.raise_for_status()
        text = resp.text
    finally:
        await client.aclose()
    for no, line in enumerate(text.splitlines(), 1):
        if marker in line:
            return {'found': True, 'path': path, 'line': no,
                    'text': line.strip()[:160]}
    return {'found': False, 'path': path, 'line': None, 'text': ''}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Живая страница панели: capture — полный HTML в файл для '
                    'зонда; serves — доехал ли маркер в раздачу.')
    ap.add_argument('mode', choices=['capture', 'serves'])
    ap.add_argument('arg', help='capture: путь страницы; serves: маркер')
    ap.add_argument('--path', default='', help='serves: где искать маркер')
    ap.add_argument('--out', default='', help='capture: куда положить файл')
    ap.add_argument('--base', default='', help='адрес панели; пусто — сам')
    ns = ap.parse_args()

    async def _go():
        if ns.mode == 'capture':
            print('файл:', await uvicorn_page_capture(ns.arg, ns.out,
                                                      base=ns.base))
        else:
            print(await uvicorn_page_serves(ns.arg, ns.path, base=ns.base))
    try:
        asyncio.run(_go())
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

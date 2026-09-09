"""
Веб-страница в PNG: рендер headless Chrome с раздача каталога и съёмкой состояний.

Зачем обёртка, если есть `google-chrome --screenshot`: три грабли съедают
ручной запуск целиком. Первая — относительные и корневые (`/public/...`)
ссылки страницы: с `file://` они не живут (у Cordova-страниц ассеты идут от
корня WebView), поэтому каталог нужно раздавать HTTP — `web_shot_serve`
поднимает `http.server` в фоновой нити с эфемерным портом и гасит его сам;
ручной `python -m http.server &` из скрипта — лотерея с осиротевшими
процессами на порту. Вторая — CSS-переходы: снимок без `--virtual-time-budget`
ловит середину анимации, и полупрозрачный в этот момент слой (sheet на 90%
из 100%) ломает все пороговые замеры пикселей рядом. Третья — состояние:
попап или модалку не открыть тапом из командной строки, поэтому `inject_js`
домешивает скрипт в копию страницы — в каталоге из симлинков, чтобы те же
корневые ссылки продолжали указывать на настоящие ассеты.

`--hide-scrollbars` включён всегда: без него Chrome рисует панель прокрутки
поверх последнего столбца макета, и ширина вьюпорта перестаёт равняться
ширине снимка (проверено: сдвиг вёрстки на 15 px при замере «1080»).
"""
import argparse
import contextlib
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import urllib.parse

# Потолок одного рендера, секунды: страница с картинками грузится секунды,
# а зависший Chrome (закрыть некому, headless) иначе повесит вызывающего.
WEB_SHOT_TIMEOUT = 90.0

# Разрешение по умолчанию — мобильный кадр телефона; для десктопной страницы
# передавайте свой `size`.
WEB_SHOT_SIZE = (1080, 2400)

# Хвост stderr Chrome в тексте ошибки: коды возврата у него малоинформативны,
# а настоящие причины (sandbox, не хватает памяти) видны только в журнале.
WEB_SHOT_ERR_TAIL = 400


def web_shot_capture(src, out, size=WEB_SHOT_SIZE, virtual_time_ms=1200,
                     inject_js='', chrome='', crop=None) -> dict:
    """
    Отрендерить страницу в PNG headless Chrome; вернуть путь и кадр.

    Args:
        src: http(s)-URL или путь к локальному HTML-файлу. Локальный файл
            раздаётся с его же каталога временным сервером — относительные и
            корневые (`/public/...`) ссылки работают, как в WebView приложения.
        out: куда положить PNG.
        size: (ширина, высота) вьюпорта — снимок ровно этого размера
            (scale-factor зафиксирован в 1, пиксель в пиксель).
        virtual_time_ms: бюджет виртуального времени Chrome: transition/
            animation досчитываются до этой отметки, кадр снимается «после».
            0 — снять сразу (поймает середину анимации).
        inject_js: JS, домешанный перед `</body>` копии страницы — открыть
            попап, выставить класс состояния, остановить таймер. Оригинал на
            диске не правится.
        chrome: путь к бинарю браузера; пусто — поиск по распространённым
            именам (google-chrome, chromium, ...).
        crop: (x0, y0, x1, y1) — поле кадра после рендера. Для листов ниже
            вьюпорта: снять длинным (`--size 1080,<scrollHeight>`) и резать;
            скроллить через inject-js нельзя — вертикальный скролл в headless
            не дорабатывается до кадра, снимок выходит пустым.

    Returns:
        {'file', 'url', 'width', 'height'} — width/height фактически
        записанного PNG (Chrome при нескруглённых значениях window-size
        может дать кадр на пиксель больше).

    Raises:
        FileNotFoundError: локальный файл не найден, браузера нет в PATH
            или Chrome не записал файл.
        RuntimeError: Chrome завершился ошибкой или не уложился в таймаут.
    """
    browser = chrome or _find_browser()
    is_url = src.startswith(('http://', 'https://'))
    if not is_url and not os.path.isfile(src):
        raise FileNotFoundError(f'страница не найдена: {src}')

    if is_url:
        _render(browser, src, out, size, virtual_time_ms)
    else:
        with _served_page(src, inject_js) as url:
            _render(browser, url, out, size, virtual_time_ms)

    from PIL import Image
    if crop:
        # PNG — lossless и пересохранение в тот же путь не плодит артефактов
        with Image.open(out) as img:
            img.crop(tuple(int(v) for v in crop)).save(out)
    with Image.open(out) as img:
        w, h = img.size
    return {'file': out, 'url': src, 'width': w, 'height': h}


@contextlib.contextmanager
def web_shot_serve(directory, port=0):
    """
    Раздать каталог HTTP на время блока; вернуть базовый URL строкой.

    Порт 0 — эфемерный (ядро выдаст свободный; конкуренты за один порт не
    дерутся), сервер — демонственная нить, `shutdown`+`server_close` при
    выходе из блока. Логи запросов заглушены: они шум в журнале проекта.

    Args:
        directory: что раздавать.
        port: фиксированный порт или 0 — свободный.

    Yields:
        str: «http://127.0.0.1:порт» без слевого слэша.
    """
    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = lambda *a, **kw: _Quiet(*a, directory=str(directory), **kw)  # noqa: E731
    with socketserver.ThreadingTCPServer(('127.0.0.1', port), handler) as srv:
        srv.daemon_threads = True
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{srv.server_address[1]}'
        finally:
            srv.shutdown()


@contextlib.contextmanager
def _served_page(path, inject_js='', head_js=''):
    """URL локальной страницы: каталог раздат; при inject_js/head_js — вариант
    в каталоге симлинков под тем же именем (ассеты продолжают резолвиться).
    head_js — скрипт сразу после <head>, он исполнится до скриптов страницы."""
    path = os.path.realpath(path)
    base, name = os.path.split(path)
    if not inject_js and not head_js:
        with web_shot_serve(base) as root:
            yield f'{root}/{urllib.parse.quote(name)}'
        return
    tmp = tempfile.mkdtemp(prefix='web_shot_')
    try:
        for entry in os.listdir(base):
            if entry != name:
                os.symlink(os.path.join(base, entry), os.path.join(tmp, entry))
        _write_variant(path, os.path.join(tmp, name), inject_js, head_js)
        with web_shot_serve(tmp) as root:
            yield f'{root}/{urllib.parse.quote(name)}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _render(browser, url, out, size, virtual_time_ms):
    """Один запуск Chrome; ошибки — исключением сразу, файл проверяем на месте."""
    args = [browser, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--no-first-run', '--no-default-browser-check',
            '--force-device-scale-factor=1',
            f'--window-size={int(size[0])},{int(size[1])}',
            f'--screenshot={os.path.abspath(out)}']
    if virtual_time_ms:
        args.append(f'--virtual-time-budget={int(virtual_time_ms)}')
    args.append(url)
    try:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=WEB_SHOT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'Chrome не завершился за {WEB_SHOT_TIMEOUT:g} с: {url}')
    if done.returncode != 0:
        raise RuntimeError(f'Chrome завершился с кодом {done.returncode}: '
                           f'{done.stderr.strip()[-WEB_SHOT_ERR_TAIL:]}')
    if not os.path.isfile(out) or os.path.getsize(out) == 0:
        raise FileNotFoundError(f'Chrome не записал снимок {out}: '
                                f'{done.stderr.strip()[-WEB_SHOT_ERR_TAIL:]}')


def _write_variant(src, dst, inject_js, head_js=''):
    """Копия HTML: head_js — скрипт сразу после открывающего <head> (посев
    localStorage до скриптов страницы), inject_js — перед последним `</body>`
    (открытие попапов, остановка таймеров; без `</body>` — в конец)."""
    with open(src, encoding='utf-8', errors='replace') as fh:
        html = fh.read()
    if head_js:
        snippet = f'<script>{head_js}</script>'
        at = re.search(r'<head[^>]*>', html, re.IGNORECASE)
        # html[at.end():] — срез; скобки «html[at.end()]» молча дали бы один
        # символ и обрезали документ до <head>
        html = (html[:at.end()] + snippet + html[at.end():] if at
                else snippet + html)
    if inject_js:
        snippet = f'<script>{inject_js}</script>'
        at = html.lower().rfind('</body>')
        html = (html[:at] + snippet + html[at:] if at != -1
                else html + snippet)
    with open(dst, 'w', encoding='utf-8') as fh:
        fh.write(html)


def _find_browser():
    """Первый найденный Chromium/Chrome; имена в порядке вероятности установки."""
    for name in ('google-chrome', 'google-chrome-stable', 'chromium',
                 'chromium-browser', 'microsoft-edge'):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError('не найден Chrome/Chromium в PATH '
                            '(google-chrome, chromium, microsoft-edge)')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Скриншот веб-страницы headless Chrome '
                    '(локальный HTML раздаётся сам).')
    ap.add_argument('src', help='URL или путь к HTML')
    ap.add_argument('--out', default='/tmp/web_shot.png', help='куда положить PNG')
    ap.add_argument('--size', default='1080,2400', help='вьюпорт шириной,высотой')
    ap.add_argument('--budget', type=int, default=1200,
                    help='virtual-time-budget мс, 0 — снять сразу')
    ap.add_argument('--inject-js', default='',
                    help='JS перед </body> копии страницы (открыть попап и т.п.)')
    ap.add_argument('--crop', default='', metavar='x0,y0,x1,y1',
                    help='подрезать кадр после рендера (длинный снимок + резка)')
    ap.add_argument('--chrome', default='', help='путь к бинарю браузера')
    ns = ap.parse_args()
    try:
        w, h = (int(v) for v in ns.size.split(',', 1))
        crop = None
        if ns.crop:
            parts = tuple(int(v) for v in ns.crop.split(',', 3))
            if len(parts) != 4:
                raise ValueError('crop: x0,y0,x1,y1')
            crop = parts
        res = web_shot_capture(ns.src, ns.out, size=(w, h),
                               virtual_time_ms=ns.budget,
                               inject_js=ns.inject_js, chrome=ns.chrome,
                               crop=crop)
        print(f"{res['file']} ({res['width']}x{res['height']})")
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

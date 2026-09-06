"""
DevTools внутри WebView: читать и двигать содержимое страницы приложения.

Третий канал к устройству, когда двух остальных мало. `uiautomator` видит
WebView одним узлом — текста и кнопок страницы в нём нет; зрение
(`adb_screen_describe`) описать такое может, но не может ни вычислить, ни
нажать, и второй раз даст другой ответ. CDP же даёт точный DOM, `eval` с
возвратом значения и навигацию — воспроизводимо, без модели.

Условия: сборка приложения обязана быть debuggable (debug-APK или
`setWebContentsDebuggingEnabled`) — иначе сокет `webview_devtools_remote_<pid>`
не поднимется. Страницы ассетов открываются схемой `https://localhost/<страница>`:
на `file://` обработчик схемы не реагирует, а `chrome://`-переходы WebView
блокирует.

Транспорт — `adb_ws` (WebSocket на stdlib), протокол — голые CDP-вызовы без
обвязки пакетов DevTools: из всего протокола этим пользователям нужны ровно
`Runtime.evaluate` и `Page.navigate`. Forward живёт до перезапуска приложения,
поэтому `adb_cdp_connect` переповешивает его на свежий pid каждый раз: висячий
forward на старый pid — типовая грабля после переустановки APK.
"""
import argparse
import json
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from adb_.adb_ import adb_run, adb_run_bytes
from adb_.adb_ws import adb_ws_open, adb_ws_recv, adb_ws_send

# Порт, на который вешается unix-сокет DevTools. Правило adb: локальный порт —
# наш, удалённый — адрес сокета приложения; конфликт с другим инструментом —
# лечится другим портом, а не другим сокетом.
ADB_CDP_PORT = 9222

# Ожидание ответа на один CDP-вызов, секунды. eval по открытой странице —
# доли секунды; секунды означает, что страница зависла или сокет достался не тот.
ADB_CDP_TIMEOUT = 10.0

# Ожидание завершения навигации, секунды. Страница ассетов грузится мгновенно;
# больше — сеть в самой странице тянет загрузку, это уже не наш случай.
ADB_CDP_NAV_TIMEOUT = 15.0

# Пауза между опросами адреса при ожидании навигации, секунды.
ADB_CDP_POLL = 0.4

# Пауза после загрузки страницы перед снимком, секунды: адрес уже правильный,
# но шрифты и фоновые картинки дорисовываются ещё мгновение.
ADB_CDP_SETTLE = 0.7


def adb_cdp_connect(package: str, serial: str = '', port: int = ADB_CDP_PORT) -> dict:
    """
    Подключить DevTools запущенного приложения: pid → forward → список страниц.

    Args:
        package: пакет приложения, в котором WebView (debuggable-сборка).
        serial: устройство; пусто — единственное подключённое.
        port: локальный порт, на который вешается сокет DevTools.

    Returns:
        {'pid': ..., 'port': ..., 'pages': [{'title','url','ws'}]}.

    Raises:
        RuntimeError: приложение не запущено, отладка WebView не включена
            или сокет DevTools не отвечает.
    """
    out = adb_run('shell', 'pidof', package, serial=serial).strip()
    if not out:
        raise RuntimeError(f'приложение {package} не запущено (pidof пуст) — сначала adb_app_start')
    pid = int(out.split()[0])
    adb_run('forward', f'tcp:{port}', f'localabstract:webview_devtools_remote_{pid}', serial=serial)
    try:
        pages = adb_cdp_pages(port)
    except RuntimeError as err:
        raise RuntimeError(
            f'DevTools {package} (pid {pid}) не отвечает: отладка WebView бывает только '
            f'в debuggable-сборке; {err}') from err
    return {'pid': pid, 'port': port, 'pages': pages}


def adb_cdp_pages(port: int = ADB_CDP_PORT) -> list[dict]:
    """
    Страницы (`type == 'page'`) текущего DevTools-списка.

    Args:
        port: локальный порт, подключённый `adb_cdp_connect`.

    Returns:
        Список {'title': заголовок, 'url': адрес, 'ws': адрес отладки цели}.

    Raises:
        RuntimeError: DevTools на порту не отвечает (не подключён или приложение мертво).
    """
    targets = _http_json(port, '/json')
    return [{'title': t.get('title', ''), 'url': t.get('url', ''),
             'ws': t.get('webSocketDebuggerUrl', '')}
            for t in targets if t.get('type') == 'page']


def adb_cdp_eval(expr: str, port: int = ADB_CDP_PORT, url_part: str = '',
                 timeout: float = ADB_CDP_TIMEOUT):
    """
    Вычислить JS в странице и вернуть значение.

    Args:
        expr: выражение; результат должен сериализоваться в JSON — на него
            живёт `returnByValue` (DOM-объекты так не вернуть, только данные).
        port: локальный порт, подключённый `adb_cdp_connect`.
        url_part: часть адреса целевой страницы; пусто — первая страница.
        timeout: секунды ожидания ответа.

    Returns:
        Значение, сериализованное страницей (dict, list, str, число...).

    Raises:
        RuntimeError: страница с таким адресом не найдена или JS бросил исключение.
        TimeoutError: страница не ответила за timeout.
    """
    sock = adb_ws_open(_socket_target(port, url_part))
    try:
        result = _cdp_call(sock, 1, 'Runtime.evaluate',
                           {'expression': expr, 'returnByValue': True,
                            'awaitPromise': True}, timeout)
    finally:
        sock.close()
    return _eval_value(result)


def adb_cdp_navigate(url: str, port: int = ADB_CDP_PORT, url_part: str = '',
                     timeout: float = ADB_CDP_NAV_TIMEOUT) -> str:
    """
    Открыть адрес в странице и дождаться, что он загрузился.

    Навигация асинхронна: `Page.navigate` отвечает сразу, до того как страница
    сменилась. Без ожидания следующий шаг уйдёт в старую страницу и примет её
    за новую.

    Args:
        url: адрес; страницы ассетов — `https://localhost/<страница>`, не `file://`.
        port: локальный порт, подключённый `adb_cdp_connect`.
        url_part: по чему узнавать завершение (часть итогового href);
            по умолчанию — путь из url.
        timeout: секунды ожидания.

    Returns:
        Итоговый адрес страницы.

    Raises:
        RuntimeError: страница не найдена.
        TimeoutError: за timeout адрес не стал ожидаемым.
    """
    needle = url_part or urlsplit(url).path
    sock = adb_ws_open(_socket_target(port))
    try:
        _cdp_call(sock, 1, 'Page.enable', {}, timeout)
        _cdp_call(sock, 2, 'Page.navigate', {'url': url}, timeout)
        deadline = time.monotonic() + timeout
        msg_id = 3
        while True:
            href = _eval_value(_cdp_call(sock, msg_id, 'Runtime.evaluate',
                                         {'expression': 'window.location.href',
                                          'returnByValue': True}, ADB_CDP_TIMEOUT))
            if needle and needle in (href or ''):
                return href
            if time.monotonic() > deadline:
                raise TimeoutError(f'навигация к {url}: href остался {href!r} через {timeout} c')
            msg_id += 1
            time.sleep(ADB_CDP_POLL)
    finally:
        sock.close()


def adb_cdp_capture_all(package: str, urls: list, out_dir: str, serial: str = '',
                        port: int = ADB_CDP_PORT, settle: float = ADB_CDP_SETTLE) -> list:
    """
    Открыть каждый адрес по очереди и снять экран; сложить кадры в каталог.

    Пачка «как выглядит каждая страница» одним вызовом: навигация, ожидание
    дорисовки, снимок и имя файла без ручной цикла-по-страницам. Снимок —
    голый PNG без нормализации JPEG: кадры берут для замеров, и сжатие не
    должно двигать яркость. Сбой одного адреса не гонит пачку — остальные
    доснимаются, ошибка лежит в строке результата.

    Args:
        package: пакет запущенного приложения (debuggable-сборка) — forward
            переповешивается на его свежий pid.
        urls: адреса; страницы ассетов — `https://localhost/<страница>`.
            Несуществующую страницу обёртка откроет диалогом ошибки, и он
            отравит следующие снимки — список адресов проверяют заранее.
        out_dir: каталог снимков; имя файла — из адреса (`menu.html` →
            `menu.png`), при коллизии дописывается счётчик, старая пачка
            не затирается.
        serial: устройство; пусто — единственное подключённое.
        port: локальный порт DevTools.
        settle: пауза после загрузки перед кадром, секунды.

    Returns:
        По записи на каждый url: {'url', 'file'} либо {'url', 'error'}.
    """
    adb_cdp_connect(package, serial=serial, port=port)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    results = []
    for url in urls:
        try:
            adb_cdp_navigate(url, port=port)
            time.sleep(settle)
            target = _shot_path(Path(out_dir), url)
            target.write_bytes(_screencap_png_raw(serial))
            results.append({'url': url, 'file': str(target)})
        except (RuntimeError, TimeoutError, OSError) as err:
            results.append({'url': url, 'error': str(err)})
    return results


def adb_cdp_element_at(x: float, y: float, port: int = ADB_CDP_PORT,
                       url_part: str = '', device_px: bool = True) -> dict:
    """
    Сказать, что за элемент под экранными координатами — ответ на «что тут».

    Координаты берут из снимка экрана (пиксели устройства): разница между
    ними и CSS-пикселями страницы делится здесь же, на devicePixelRatio.
    Без этого элемент ищется в (x/3, y/3) от того места, куда смотрят.

    Args:
        x, y: координаты; по умолчанию — пиксели снимка.
        port: локальный порт DevTools.
        url_part: часть адреса целевой страницы; пусто — первая страница.
        device_px: False — координаты уже CSS-пиксели, делить не надо.

    Returns:
        {'tag', 'id', 'cls', 'text' (первые 80 символов), 'href',
         'rect': {'x','y','w','h'} в CSS-пикселях, 'chain' — теги от
        элемента вверх} или None, если под координатами ничего нет.
    """
    js = _ELEMENT_AT_JS % (float(x), float(y), 'true' if device_px else 'false')
    return adb_cdp_eval(js, port=port, url_part=url_part)


def adb_cdp_element_rect(selector: str, port: int = ADB_CDP_PORT,
                         url_part: str = '', serial: str = '') -> dict:
    """
    Прямоугольники элементов по CSS-селектору — и в CSS-пикселях, и в пикселях
    снимка экрана. Обратный ход к `adb_cdp_element_at`: тот ищет элемент
    под экранным пикселем, этот говорит, где на снимке лежит элемент.

    Нужен, чтобы проверять картинку по адресу: «замерь фон под этой крышкой
    текста» без ручной арифметики vw→px и без угадывания, где крышка встала
    после поворота вёрстки. Экранные координаты считаются по devicePixelRatio
    с поправкой на начало WebView на экране — сдвиг статус-бара берётся
    из окон системы (`_status_bar_height`), DOM его не знает. Статус-бар
    на эмуляторе (Android 16) даёт 66 px, на телефоне — 91 px; один промах
    на эту высоту и профиль меряет не там.

    Args:
        selector: CSS-селектор; совпадение ищет `querySelectorAll` —
            возвращает все элементы, не только первый.
        port: локальный порт DevTools.
        url_part: часть адреса целевой страницы; пусто — первая страница.
        serial: устройство для чтения статуса-бара; пусто — единственное.

    Returns:
        {'selector', 'count', 'dpr', 'origin': {'x','y'} — сдвиг WebView
         на экране, 'items': [{'tag','cls','text' (первые 40 символов),
         'css': {'x','y','w','h'}, 'screen': {'x','y','w','h'}}]};
         items пуст, если селектор ничего не нашёл.
    """
    js = _ELEMENT_RECT_JS % json.dumps(selector)
    result = adb_cdp_eval(js, port=port, url_part=url_part) or {}
    origin_y = _status_bar_height(serial)
    dpr = result.get('dpr', 1) or 1
    for item in result.get('items', []):
        css = item['css']
        item['screen'] = {'x': round(css['x'] * dpr), 'y': round((css['y'] + origin_y / dpr) * dpr),
                          'w': round(css['w'] * dpr), 'h': round(css['h'] * dpr)}
    result['selector'] = selector
    result['origin'] = {'x': 0, 'y': origin_y}
    return result


# docstring для eval: прямоугольник считается страницей — dpr и прокрутка
# берются с неё же. Селектор подставляется через json.dumps: кавычки и
# скобки селектора не ломают выражение.
_ELEMENT_RECT_JS = """
((sel) => {
  const els = document.querySelectorAll(sel);
  const d = window.devicePixelRatio || 1;
  const items = [];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    items.push({tag: el.tagName.toLowerCase(),
                cls: typeof el.className === 'string' ? el.className : '',
                text: (el.textContent || '').trim().slice(0, 40),
                css: {x: +(r.x + window.scrollX).toFixed(1),
                      y: +(r.y + window.scrollY).toFixed(1),
                      w: +r.width.toFixed(1), h: +r.height.toFixed(1)}});
  }
  return {count: items.length, dpr: d, items: items};
})(%s)
"""


# Окно статус-бара в выводе dumpsys: строка вида
# `mAttrs={(0,0)(fillx66) gr=TOP ... ty=STATUS_BAR` — 66 есть высота бара
# в физических пикселях, на столько WebView начинается ниже верха снимка.
# DOM этого сдвига не знает; на эмуляторе (Android 16) бар 66 px, на телефоне —
# 91 px, и промах на эту высоту означает «профиль мерит не там».
_STATUS_BAR_RE = re.compile(r'\((\d+),(\d+)\)\((\S+)x(\d+)\).*ty=STATUS_BAR')


def _status_bar_height(serial: str) -> int:
    """Высота статус-бара в физических пикселях по списку окон; 0 — если не нашёлся."""
    out = adb_run('shell', 'dumpsys window windows | grep -m1 ty=STATUS_BAR', serial=serial)
    match = _STATUS_BAR_RE.search(out)
    return int(match.group(4)) if match else 0


def _screencap_png_raw(serial: str) -> bytes:
    """Голый PNG со снимка экрана; JPEG-нормализация не нужна — кадры для замеров."""
    png = adb_run_bytes('exec-out', 'screencap', '-p', serial=serial)
    if not png.startswith(b'\x89PNG'):
        raise RuntimeError('screencap: вывод не похож на PNG (устройство спит?)')
    return png


def _shot_path(out_dir: Path, url: str) -> Path:
    """Имя файла снимка из адреса; существующее имя получает счётчик."""
    name = urlsplit(url).path.rsplit('/', 1)[-1].rsplit('.', 1)[0] or 'page'
    name = re.sub(r'[^0-9A-Za-z._-]', '-', name)
    target = out_dir / f'{name}.png'
    n = 1
    while target.exists():
        target = out_dir / f'{name}-{n}.png'
        n += 1
    return target


# docstring для eval: одна страница считает элемент сама — только так
# devicePixelRatio берётся с неё, а не догадкой с хоста. Шаблоны подставляют
# числа до вставки в выражение; % в JS нет, форматирование безопасное.
_ELEMENT_AT_JS = """
((x, y, dev) => {
  const d = dev ? (window.devicePixelRatio || 1) : 1;
  const el = document.elementFromPoint(x / d, y / d);
  if (!el) return null;
  const chain = [];
  for (let e = el; e && chain.length < 6; e = e.parentElement)
    chain.push(e.tagName.toLowerCase());
  const r = el.getBoundingClientRect();
  return {tag: el.tagName.toLowerCase(), id: el.id || '',
          cls: typeof el.className === 'string' ? el.className : '',
          text: (el.textContent || '').trim().slice(0, 80),
          href: el.getAttribute('href') || '',
          rect: {x: Math.round(r.x), y: Math.round(r.y),
                 w: Math.round(r.width), h: Math.round(r.height)},
          chain: chain};
})(%s, %s, %s)
"""


def _cdp_call(sock, msg_id: int, method: str, params: dict, timeout: float) -> dict:
    """Один вызов CDP: события по дороге пропускаются, error — исключение."""
    sock.settimeout(timeout)
    adb_ws_send(sock, json.dumps({'id': msg_id, 'method': method, 'params': params}))
    while True:
        try:
            answer = json.loads(adb_ws_recv(sock))
        except TimeoutError as err:
            raise TimeoutError(f'{method}: DevTools не ответил за {timeout} c') from err
        if answer.get('id') == msg_id:
            if 'error' in answer:
                raise RuntimeError(f"{method}: {answer['error'].get('message', answer['error'])}")
            return answer.get('result', {})


def _http_json(port: int, path: str) -> list:
    """GET к DevTools-эндпоинту с JSON-ответом."""
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}')
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))
    except OSError as err:
        raise RuntimeError(f'DevTools на порту {port} не отвечает (нужен adb_cdp_connect): {err}') from err


def _socket_target(port: int, url_part: str = '') -> str:
    """Адрес отладки целевой страницы; пусто в url_part — первой страницы."""
    pages = [p for p in adb_cdp_pages(port) if p['ws']]
    if url_part:
        pages = [p for p in pages if url_part in p['url']]
    if not pages:
        seen = ', '.join(p['url'] for p in adb_cdp_pages(port) if p['ws']) or 'ни одной страницы'
        raise RuntimeError(f'страница {url_part!r} не найдена; доступны: {seen}')
    return pages[0]['ws']


def _eval_value(result: dict):
    """Значение из ответа Runtime.evaluate; исключение страницы — наше исключение."""
    details = result.get('exceptionDetails')
    if details:
        raised = details.get('exception', {}).get('description') or details.get('text', '')
        raise RuntimeError(f'JS бросил исключение: {raised}')
    return result.get('result', {}).get('value')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='DevTools внутри WebView: читать и двигать страницы приложения.',
        epilog="connect com.example.app | pages | eval 'location.href' | "
               "navigate https://localhost/menu.html | "
               "capture com.example.app shots/ https://localhost/a.html https://localhost/b.html | "
               "element 540 300 | element-rect .card-cover")
    parser.add_argument('command', choices=['connect', 'pages', 'eval', 'navigate',
                                            'capture', 'element', 'element-rect'])
    parser.add_argument('args', nargs='*', help='пакет / JS / адрес / снимки / координаты')
    parser.add_argument('--serial', default='', help='устройство для connect и снимков')
    parser.add_argument('--port', type=int, default=ADB_CDP_PORT, help='локальный порт DevTools')
    parser.add_argument('--url-part', default='', help='часть адреса целевой страницы')
    parser.add_argument('--settle', type=float, default=ADB_CDP_SETTLE,
                        help='пауза после загрузки перед кадром, capture')
    ns = parser.parse_args()

    try:
        if ns.command == 'connect':
            print(adb_cdp_connect(ns.args[0], serial=ns.serial, port=ns.port))
        elif ns.command == 'pages':
            for page in adb_cdp_pages(port=ns.port):
                print(page['url'], '—', page['title'])
        elif ns.command == 'eval':
            print(adb_cdp_eval(ns.args[0], port=ns.port, url_part=ns.url_part))
        elif ns.command == 'capture':
            for row in adb_cdp_capture_all(ns.args[0], ns.args[2:], ns.args[1],
                                           serial=ns.serial, port=ns.port, settle=ns.settle):
                print(row.get('file') or f"{row['url']}: ОШИБКА {row['error']}")
        elif ns.command == 'element':
            print(adb_cdp_element_at(float(ns.args[0]), float(ns.args[1]),
                                     port=ns.port, url_part=ns.url_part))
        elif ns.command == 'element-rect':
            r = adb_cdp_element_rect(ns.args[0], port=ns.port,
                                     url_part=ns.url_part, serial=ns.serial)
            print(f"{r['selector']}: {r['count']} совпадений, dpr={r.get('dpr')}, "
                  f"origin y={r['origin']['y']}")
            for item in r['items']:
                css, scr = item['css'], item['screen']
                print(f"  <{item['tag']} .{item['cls']}> «{item['text']}» "
                      f"css=({css['x']},{css['y']} {css['w']}×{css['h']}) "
                      f"screen=({scr['x']},{scr['y']} {scr['w']}×{scr['h']})")
        else:
            print(adb_cdp_navigate(ns.args[0], port=ns.port, url_part=ns.url_part))
    except (IndexError, ValueError) as err:
        raise SystemExit(f'ошибка в аргументах: {err}')
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')

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
import time
import urllib.request
from urllib.parse import urlsplit

from adb_.adb_ import adb_run
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
        epilog="connect com.example.app | pages | eval 'location.href' | navigate https://localhost/menu.html")
    parser.add_argument('command', choices=['connect', 'pages', 'eval', 'navigate'])
    parser.add_argument('args', nargs='*', help='пакет / JS / адрес')
    parser.add_argument('--serial', default='', help='устройство для connect')
    parser.add_argument('--port', type=int, default=ADB_CDP_PORT, help='локальный порт DevTools')
    parser.add_argument('--url-part', default='', help='часть адреса целевой страницы')
    ns = parser.parse_args()

    try:
        if ns.command == 'connect':
            print(adb_cdp_connect(ns.args[0], serial=ns.serial, port=ns.port))
        elif ns.command == 'pages':
            for page in adb_cdp_pages(port=ns.port):
                print(page['url'], '—', page['title'])
        elif ns.command == 'eval':
            print(adb_cdp_eval(ns.args[0], port=ns.port, url_part=ns.url_part))
        else:
            print(adb_cdp_navigate(ns.args[0], port=ns.port, url_part=ns.url_part))
    except (IndexError, ValueError) as err:
        raise SystemExit(f'ошибка в аргументах: {err}')
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')

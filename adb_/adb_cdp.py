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
import base64
import json
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from adb_.adb_ import adb_run, adb_run_bytes, adb_screen_size
from adb_.adb_ui import adb_ui_nodes
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

# Ожидание JS-условия по умолчанию, секунды, и шаг опроса. Страница
# докладывает состояние (анимация, рендер, загрузка) за доли секунды;
# фиксированный sleep в скриптах либо ждёт лишнего, либо не дожидается —
# отсюда опрос с условием вместо пауз.
ADB_CDP_WAIT_TIMEOUT = 10.0
ADB_CDP_WAIT_POLL = 0.3

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


def adb_cdp_storage(items: dict, url_part: str = '', reload: bool = False,
                    port: int = ADB_CDP_PORT) -> dict:
    """
    Залить `items` в localStorage целевой страницы и прочитать обратно.

    Сценарии сверки начинаются с шины: персона в `diaData`, режимы, флаги.
    Настраивать их тапами по секретному редактору — дольше и менее
    воспроизводимо, чем писать в то же место, откуда приложение читает.
    Значения кладутся как `JSON.stringify` (контракт шины: читают
    `JSON.parse`), поэтому число возвращается числом, а строка с кавычками
    не ломается. `reload` перезагружает страницу — приложение читает шину
    на старте; чтение обратно делается до перезагрузки, после неё гонка.

    Args:
        items: {ключ: значение} — сериализуемое в JSON. Строка, похожая на
            готовый JSON (начинается с `{` или `[`), — ошибка: сериализует
            эта функция, такая строка закодировалась бы дважды.
        url_part: часть адреса целевой страницы; пусто — первая страница.
        reload: перезагрузить страницу после записи (с settle-паузой).
        port: локальный порт, подключённый `adb_cdp_connect`.

    Returns:
        {'seeded': [ключи], 'values': {ключ: строка из хранилища}} —
        значения обратного чтения сырые, распаковывает вызывающий.

    Raises:
        ValueError: значение — строка вида JSON; приложение прочитало бы
            строку вместо объекта и молча live на дефолтах шины.
    """
    # Проверка до первого обращения к странице: двойное кодирование
    # живучее — ошибка тоньше, чем пустой экран, и гадать по живому приложению
    # «почему diaData строка» дороже, чем отклонить вызов на пороге.
    for key, value in items.items():
        if isinstance(value, str) and value.lstrip()[:1] in ('{', '['):
            raise ValueError(
                f'ключ {key!r}: строка похожа на готовый JSON — storage '
                'сериализует сам, передайте объект либо распарсенное '
                'значение (json.loads)')
    adb_cdp_eval(_storage_js(items), port=port, url_part=url_part)
    keys = [str(k) for k in items]
    got = adb_cdp_eval(
        'JSON.stringify(Object.fromEntries(' + json.dumps(keys, ensure_ascii=False) +
        '.map(k => [k, localStorage.getItem(k)])))', port=port, url_part=url_part)
    values = json.loads(got) if isinstance(got, str) else dict(got)
    if reload:
        # reload асинхронен: выражение вернётся до выгрузки документа,
        # осесть даём settle-паузой, чтобы следующий вызов не поймал разгрузку
        adb_cdp_eval('location.reload()', port=port, url_part=url_part)
        time.sleep(ADB_CDP_SETTLE)
    return {'seeded': keys, 'values': values}


def adb_cdp_waitfor(expr: str, port: int = ADB_CDP_PORT, url_part: str = '',
                    timeout: float = ADB_CDP_WAIT_TIMEOUT, poll: float = ADB_CDP_WAIT_POLL):
    """
    Опрашивать `expr`, пока значение не станет истинным, и вернуть его.

    Замена слепому пауза-then-проверка: условие — любое JS-выражение,
    истинность — по правилам JS (`''`, `0`, `null`, `undefined` — ложь),
    дождавшееся значение возвращается, чтобы результат можно было проверить
    сразу тем же вызовом. На время навигации страница исчезает на мгновение —
    ошибку «страница не найдена» ожидание пережидает, а не считает провалом.

    Args:
        expr: выражение; как у `adb_cdp_eval`, значение должно сериализоваться.
        port: локальный порт, подключённый `adb_cdp_connect`.
        url_part: часть адреса целевой страницы; пусто — первая страница.
        timeout: секунды ожидания условия.
        poll: шаг опроса, секунды.

    Returns:
        Первое истинное значение выражения.

    Raises:
        TimeoutError: за timeout условие не стало истинным; в сообщении
            последнее значение (или последний диагноз eval).
        RuntimeError: сокет не поднят (к `port` никто не подключался).
    """
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            last = adb_cdp_eval(expr, port=port, url_part=url_part)
            if last:
                return last
        except RuntimeError as err:
            last = f'ошибка eval: {err}'
        if time.monotonic() > deadline:
            raise TimeoutError(f'waitfor {expr!r}: не дождался за {timeout} c, '
                               f'последнее значение {last!r}')
        time.sleep(poll)


def adb_cdp_navigate(url: str, port: int = ADB_CDP_PORT, url_part: str = '',
                     timeout: float = ADB_CDP_NAV_TIMEOUT, waitfor: str = '') -> str:
    """
    Открыть адрес в странице и дождаться, что он загрузился.

    Навигация асинхронна: `Page.navigate` отвечает сразу, до того как страница
    сменилась. Без ожидания следующий шаг уйдёт в старую страницу и примет её
    за новую.

    Завершение узнаётся по игле — подстроке ИТОГОВОГО адреса (после
    редиректов), а не запрошенного: страница входа может осесть на
    `reserve.html`, и игла `auth` тут никогда не сработает. При этом точное
    совпадение href с запрошенным адресом засчитывается всегда, какой бы
    иглы ни просили: возврат на текущую страницу — успех, а не провал
    (ложные 15-секундные паузы на этом месте — измеренная цена старого
    правила).

    Args:
        url: адрес; страницы ассетов — `https://localhost/<страница>`, не `file://`.
        port: локальный порт, подключённый `adb_cdp_connect`.
        url_part: игла завершения — часть итогового href; по умолчанию путь из url.
        timeout: секунды ожидания.
        waitfor: JS-условие, которого дождаться после смены адреса (пусто — не
            ждать). Адрес сам по себе ещё не готовность: данные страница
            дораскладывает после смены href.

    Returns:
        Итоговый адрес страницы.

    Raises:
        RuntimeError: страница не найдена.
        TimeoutError: за timeout адрес не стал ожидаемым (или не истинно waitfor).
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
            if href == url or (needle and needle in (href or '')):
                if waitfor:
                    # Ждём на ИТОГОВОМ адресе: до-навигационная выборка и игла
                    # описывают прошлую страницу — после редиректа подстрока
                    # 'auth' на новой странице не встретится никогда.
                    adb_cdp_waitfor(waitfor, port=port, url_part=href or needle or url_part)
                return href
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f'навигация к {url}: href остался {href!r} через {timeout} c — '
                    f'игла {needle!r} сверяется с ИТОГОВЫМ адресом страницы '
                    f'(после редиректов), проверь, куда страница легла на самом деле')
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


def adb_cdp_target_pick(url_part: str, port: int = ADB_CDP_PORT) -> dict:
    """
    Выбрать цель DevTools по части адреса — одну и ровно ту, что названа.

    `adb_cdp_pages` при открытых нескольких страницах отдаёт список, а
    «первая страница» при живом оффскрин-WebView (свой `evod_pdf.html` плюс
    видимый `reserve.html`) — лотерея: eval уезжает не туда и молчит
    правильным ответом не на тот вопрос.

    Args:
        url_part: подстрока адреса цели (`reserve`, `evod_pdf`).
        port: локальный порт, подключённый `adb_cdp_connect`.

    Returns:
        Строка страницы `{'title','url','ws'}`.

    Raises:
        RuntimeError: ни одна страница не подошла или подошли несколько —
            в сообщении перечислены доступные адреса.
    """
    return _pick_target(adb_cdp_pages(port), url_part)


def adb_cdp_viewport(url_part: str = '', serial: str = '',
                     port: int = ADB_CDP_PORT) -> dict:
    """
    geometrie страницы глазами самого WebView: dpr, CSS-размер и сдвиги кадра.

    Страница приложения видит не весь кадр экрана: сверху съеден статус-бар,
    снизу — панель навигации, и обе полосы надо учесть. CSS-пиксели
    страницы переводятся в экранные умножением на `dpr` и сдвигом `(x0, y0)`;
    `dpr` и CSS-размер называет сама страница, а сдвиг — uiautomator по окну
    WebView: сам WebView о своём положении не знает (`screenTop` = 0).

    Args:
        url_part: часть адреса целевой страницы; пусто — первая страница.
        serial: устройство; пусто — единственное подключённое.
        port: локальный порт, подключённый `adb_cdp_connect`.

    Returns:
        `{'dpr', 'css_w', 'css_h', 'screen_w', 'screen_h', 'x0', 'y0'}`;
        `x0`/`y0` — левый верхний угол страницы в пикселях экрана.
    """
    view = adb_cdp_eval(_VIEWPORT_JS, port=port, url_part=url_part)
    if not view:
        raise RuntimeError('страница не отдала геометрию (documentElement мёртв?)')
    screen_w, screen_h = adb_screen_size(serial=serial)
    x0, y0 = _webview_origin(serial)
    dpr, css_w, css_h = float(view['dpr']), float(view['w']), float(view['h'])
    return {'dpr': dpr, 'css_w': css_w, 'css_h': css_h,
            'screen_w': screen_w, 'screen_h': screen_h, 'x0': x0, 'y0': y0}


def adb_cdp_tap(selector: str, url_part: str = '', serial: str = '',
                port: int = ADB_CDP_PORT) -> tuple:
    """
    Нажать на элемент страницы — координаты считает сам WebView.

    `uiautomator` не видит узлов WebView, а тап в угаданную точку экрана
    промахивается на статус-бар и масштаб. Здесь центр элемента берётся из
    `getBoundingClientRect` целевой страницы и переводит в экранные пиксели
    калибровкой из `adb_cdp_viewport`.

    Событие доставляется напрямую в страницу через CDP Input, а не системным
    `input tap`: тот идёт через SurfaceFlinger и попадает окну только после
    отрисованного кадра — на холодном, загруженном WebView клик доживает до
    обработчика 3–5 с, и любая проверка «нажал — открылось» превращается в
    лотерею. Доставка в тот же рендерер, где читали прямоугольник, идёт
    столько же, сколько eval.

    Args:
        selector: CSS-селектор, передаётся в `querySelector` как есть.
        url_part: часть адреса целевой страницы; пусто — первая страница.
        serial: устройство; нужно для калибровки возвращаемых координат.
        port: локальный порт, подключённый `adb_cdp_connect`.

    Returns:
        `(x, y)` — экранные пиксели, куда попал тап (для сверки со снимком).

    Raises:
        RuntimeError: элемент не найден или невидим (нулевой прямоугольник).
    """
    js = _TAP_JS % json.dumps(selector, ensure_ascii=False)
    rect = adb_cdp_eval(js, port=port, url_part=url_part)
    if not rect:
        raise RuntimeError(f'элемент {selector!r} не найден на странице')
    if rect['w'] <= 0 or rect['h'] <= 0:
        raise RuntimeError(f'элемент {selector!r} невидим (нулевой прямоугольник)')
    vp = adb_cdp_viewport(url_part=url_part, serial=serial, port=port)
    _cdp_touch(port, url_part, rect['x'], rect['y'])
    return int(round(vp['x0'] + rect['x'] * vp['dpr'])), int(round(vp['y0'] + rect['y'] * vp['dpr']))


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


def adb_cdp_element_shot(selector: str, out: str, port: int = ADB_CDP_PORT,
                         url_part: str = '', serial: str = '') -> dict:
    """
    Снять элемент по селектору отдельным png — кадр ровно по границам элемента.

    Это выкройка для `image_seam`/`image_rect_seams`/`image_audit`: в полном
    снимке каждый раз надо не испортить вычитание статус-бара, а здесь
    координаты берёт `adb_cdp_element_rect`, и крой просит странице в её
    CSS-пикселях с масштабом devicePixelRatio — на выходе физические пиксели,
    те самые, которыми меряют инструменты. Отдельный кадр ещё и тем удобен,
    что `image_diff_zones` сворачивает его с эталоном без ручной обрезки.

    Args:
        selector: CSS-селектор; берут первое совпадение.
        out: файл для записи; если заканчивается `/` (или это существующий
            каталог) — имя выводят из селектора (не-буквы-и-цифры в дефисы).
        port: локальный порт DevTools.
        url_part: часть адреса целевой страницы; пусто — первая страница.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        {'file': записанный путь, 'rect': экранный прямоугольник
         {'x','y','w','h'}} элемента.

    Raises:
        RuntimeError: селектор ничего не нашёл или страница не отдала кадр.
        TimeoutError: страница не ответила за timeout.
    """
    rect = adb_cdp_element_rect(selector, port=port, url_part=url_part, serial=serial)
    if not rect['items']:
        raise RuntimeError(f'селектор {selector!r} не нашёл ничего — проверь адрес и вёрстку')
    css, screen = rect['items'][0]['css'], rect['items'][0]['screen']
    target = Path(out)
    if out.endswith('/') or target.is_dir():
        name = re.sub(r'[^0-9A-Za-z]+', '-', selector).strip('-').lower() or 'element'
        target.parent.mkdir(parents=True, exist_ok=True)
        target = target / f'{name}.png'
    sock = adb_ws_open(_socket_target(port, url_part))
    try:
        # captureBeyondViewport (по умолчанию true) уже домножает кадр на
        # devicePixelRatio: scale: dpr удвоил бы степень и выкройка вышла бы
        # чужого размера. Держим scale=1 и явно просим за границами вьюпорта —
        # так выходит ровно css×dpr, то есть экранный прямоугольник элемента.
        shot = _cdp_call(sock, 1, 'Page.captureScreenshot',
                         {'format': 'png', 'captureBeyondViewport': True,
                          'clip': {'x': css['x'], 'y': css['y'],
                                   'width': css['w'], 'height': css['h'],
                                   'scale': 1.0}}, ADB_CDP_TIMEOUT)
    finally:
        sock.close()
    if not shot.get('data'):
        raise RuntimeError('Page.captureScreenshot вернул пустой кадр')
    png = base64.b64decode(shot['data'])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    # Chrome берёт целые пиксели с округлением вниз, и кадр выходит на 1–2 px
    # короче теоретического css×dpr. Прямоугольник в ответе описывает записанный
    # кадр (ширина и высота PNG лежат в IHDR, big-endian со смещения 16 —
    # Pillow ради двух чисел сюда не нужен), чтобы seam не мерил за его краем.
    w_px, h_px = int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')
    return {'file': str(target),
            'rect': {'x': screen['x'], 'y': screen['y'], 'w': w_px, 'h': h_px}}


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

# Геометрия страницы — её же глазами: dpr у WebView отличается от
# `wm density` системы, а CSS-размер знает только сам документ.
_VIEWPORT_JS = "({dpr: window.devicePixelRatio || 1, w: innerWidth, h: innerHeight})"

# Центр элемента в CSS-пикселях страницы; null — селектор никого не нашёл.
_TAP_JS = """
(() => {
  const el = document.querySelector(%s);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height};
})()
"""


def _webview_origin(serial: str) -> tuple:
    """
    Верхний левый угол окна WebView — его знает uiautomator, а не страница.

    Сам WebView о своём сдвиге молчит (`screenTop` = 0, `screen.height` уже
    делится на dpr), а арифметика `screen_h − dpr·css_h` предполагает, что
    всё несъеденное место — сверху; на реальном Reserve сверху статус-бар,
    снизу панель навигации, и остаток делится между ними пополам. Окно
    берём из дампа: среди WebView-узлов крупнейшего (мелкие — чужие врезки).
    """
    views = [n for n in adb_ui_nodes(serial=serial)
             if n['class'] == 'android.webkit.WebView' and n['bounds']]
    if not views:
        raise RuntimeError('на экране нет окна WebView (uiautomator) — приложение открыто?')
    bounds = max(views, key=lambda n: (n['bounds'][2] - n['bounds'][0])
                 * (n['bounds'][3] - n['bounds'][1]))['bounds']
    return bounds[0], bounds[1]


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


def _cdp_touch(port: int, url_part: str, x: float, y: float) -> None:
    """
    Касание страницы через CDP Input: touchStart + touchEnd в css-пикселях.

    Системный `input tap` SurfaceFlinger отдаёт окну только после отрисованного
    им кадра, и холодный WebView держит клик в очереди секундами; здесь событие
    идёт прямо в рендерер — та же страница, та же очередь, что у eval.
    """
    sock = adb_ws_open(_socket_target(port, url_part))
    try:
        _cdp_call(sock, 1, 'Input.dispatchTouchEvent',
                  {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]}, ADB_CDP_TIMEOUT)
        _cdp_call(sock, 2, 'Input.dispatchTouchEvent',
                  {'type': 'touchEnd', 'touchPoints': []}, ADB_CDP_TIMEOUT)
    finally:
        sock.close()


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
    if url_part:
        return adb_cdp_target_pick(url_part, port)['ws']
    pages = [p for p in adb_cdp_pages(port) if p['ws']]
    if not pages:
        raise RuntimeError('ни одной страницы с ws в DevTools — приложение мертво?')
    return pages[0]['ws']


def _pick_target(pages: list, url_part: str) -> dict:
    """
    Ровно одна страница из списка по подстроке адреса; кандидаты — в ошибку.

    Молча взять первую из двух подошедших — тот же грех, что и «первая
    страница» при живом оффскрин-WebView: ответ приходит, но не от той цели.
    """
    hits = [p for p in pages if p.get('ws') and url_part in p['url']]
    if not hits:
        seen = ', '.join(p['url'] for p in pages if p.get('ws')) or 'ни одной страницы'
        raise RuntimeError(f'страница {url_part!r} не найдена; доступны: {seen}')
    if len(hits) > 1:
        raise RuntimeError(f'выбор цели {url_part!r} неоднозначно: '
                           + ', '.join(p['url'] for p in hits)
                           + ' — уточните подстроку')
    return hits[0]


def _storage_js(items: dict) -> str:
    """
    JS записи items в localStorage.

    Двойное json.dumps: внешнее превращает JSON значения в строковый
    литерал JS — в хранилище кладётся ровно то, что вернул бы
    JSON.stringify в самой странице (кириллица остаётся читаемой).
    """
    return ';'.join(
        f'localStorage.setItem({json.dumps(str(k))}, '
        f'{json.dumps(json.dumps(v, ensure_ascii=False), ensure_ascii=False)})'
        for k, v in items.items())


def _eval_value(result: dict):
    """Значение из ответа Runtime.evaluate; исключение страницы — наше исключение."""
    details = result.get('exceptionDetails')
    if details:
        raised = details.get('exception', {}).get('description') or details.get('text', '')
        raise RuntimeError(f'JS бросил исключение: {raised}')
    return result.get('result', {}).get('value')


def _cli_out(value, as_json: bool) -> str:
    """
    Формат вывода значения: без флага — как print, с `--json` — JSON.

    Головой print даёт Python-repr: `True`, `None`, одночные кавычки у
    строк — `jq` и shell-скрипты это не едят, и каждый дописывал свой
    постпроцессинг. `--json` отдаёт `true`/`false`/`null` и двойные кавычки;
    `ensure_ascii=False` — чтобы кириллица оставалась читаемой.
    """
    return json.dumps(value, ensure_ascii=False) if as_json else str(value)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='DevTools внутри WebView: читать и двигать страницы приложения.',
        epilog="connect com.example.app | pages | eval 'location.href' | "
               "waitfor 'window.ready' | "
               "navigate https://localhost/menu.html --waitfor 'document.title' | "
               "capture com.example.app shots/ https://localhost/a.html https://localhost/b.html | "
               "element 540 300 | element-rect .card-cover | element-shot .card-cover shots/cover.png | "
               "target reserve | viewport | tap '#download' | "
               "storage diaData='{\"name\":\"СКУБА\"}' --reload")
    parser.add_argument('command', choices=['connect', 'pages', 'target', 'eval', 'navigate',
                                            'waitfor', 'capture', 'element', 'element-rect',
                                            'element-shot', 'viewport', 'tap', 'storage'])
    parser.add_argument('args', nargs='*', help='пакет / JS / адрес / снимки / координаты')
    parser.add_argument('--serial', default='', help='устройство для connect и снимков')
    parser.add_argument('--port', type=int, default=ADB_CDP_PORT, help='локальный порт DevTools')
    parser.add_argument('--url-part', default='', help='часть адреса целевой страницы')
    parser.add_argument('--settle', type=float, default=ADB_CDP_SETTLE,
                        help='пауза после загрузки перед кадром, capture')
    parser.add_argument('--timeout', type=float, default=0.0,
                        help='секунды ожидания (navigate, waitfor); 0 — значение по умолчанию команды')
    parser.add_argument('--waitfor', default='',
                        help='для navigate: JS-условие, которого дождаться после смены адреса')
    parser.add_argument('--json', action='store_true',
                        help='вывести значение как JSON: true/false/null вместо True/False/None')
    parser.add_argument('--reload', action='store_true',
                        help='для storage: перезагрузить страницу после записи')
    ns = parser.parse_args()

    try:
        if ns.command == 'connect':
            print(adb_cdp_connect(ns.args[0], serial=ns.serial, port=ns.port))
        elif ns.command == 'pages':
            for page in adb_cdp_pages(port=ns.port):
                print(page['url'], '—', page['title'])
        elif ns.command == 'target':
            print(adb_cdp_target_pick(ns.args[0], port=ns.port))
        elif ns.command == 'viewport':
            print(adb_cdp_viewport(url_part=ns.url_part, serial=ns.serial, port=ns.port))
        elif ns.command == 'tap':
            print(adb_cdp_tap(ns.args[0], url_part=ns.url_part, serial=ns.serial, port=ns.port))
        elif ns.command == 'eval':
            print(_cli_out(adb_cdp_eval(ns.args[0], port=ns.port, url_part=ns.url_part), ns.json))
        elif ns.command == 'storage':
            items = {}
            for pair in ns.args:
                key, sep, raw = pair.partition('=')
                if not sep or not key:
                    raise SystemExit(f'storage: ждём КЛЮЧ=ЗНАЧЕНИЕ-в-JSON, получил {pair!r}')
                items[key] = json.loads(raw)
            out = adb_cdp_storage(items, url_part=ns.url_part, reload=ns.reload,
                                  port=ns.port)
            print('записано:', ', '.join(out['seeded']),
                  '— страница перезагружена' if ns.reload else '')
        elif ns.command == 'waitfor':
            value = adb_cdp_waitfor(ns.args[0], port=ns.port, url_part=ns.url_part,
                                    timeout=ns.timeout or ADB_CDP_WAIT_TIMEOUT)
            print(_cli_out(value, ns.json))
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
        elif ns.command == 'element-shot':
            shot = adb_cdp_element_shot(ns.args[0], ns.args[1], port=ns.port,
                                        url_part=ns.url_part, serial=ns.serial)
            r = shot['rect']
            print(f"{shot['file']}: screen=({r['x']},{r['y']} {r['w']}×{r['h']})")
        elif ns.command == 'navigate':
            print(adb_cdp_navigate(ns.args[0], port=ns.port, url_part=ns.url_part,
                                   timeout=ns.timeout or ADB_CDP_NAV_TIMEOUT,
                                   waitfor=ns.waitfor))
    except (IndexError, ValueError) as err:
        raise SystemExit(f'ошибка в аргументах: {err}')
    except (RuntimeError, TimeoutError) as err:
        raise SystemExit(f'ошибка: {err}')

"""
Прогон веб-страницы головой: выполнить JS на загруженной странице и прочитать,
что вышло — состояние, хранилище, консоль; картинкой, не только снимком.

`web_shot` рисует страницу, но отвечает только «как выглядит». На вопросы
«что делает обработчик» он не годится в принципе: файловый input снимком не
накормишь, `FileReader` на снимке не дочитается, а `localStorage` после событий
со скриншота не читается. Здесь тот же headless Chrome, но с
`--remote-debugging-port` и CDP: страница живёт, в неё идёт `Runtime.evaluate`
с `awaitPromise`, обратно приходит значение, а `console.*` и падения
страницы собираются событиями по дороге.

Почему CDP, а не трюк с `--dump-dom` и внедрённым скриптом: дамп DOM
стреляет затвором по виртуальному времени, и настоящая асинхронщина
(`FileReader`, загрузка картинки для `naturalWidth`) в бюджет не попадает —
колбэк не наступает, результат пустой, и полчаса уходят на поиски бага там,
где его нет (проверено на обработчике загрузки QR: голый дамп показывал
пустоту, CDP — полный отчёт).

Каталог страницы раздаётся `web_shot_serve`, а не `file://`: корневые
ссылки (`/public/...`) на `file://` не живут, а локальный origin хранилища
должен совпадать с тем, под которым страницу откроет приложение.

Транспорт WebSocket — `adb_.adb_ws`: кадр RFC6455 без знаний об Android,
писать второй за руку с ним — тот грех, что запрещает `tool_rules` (§1).
"""
import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time

from adb_.adb_ws import adb_ws_open, adb_ws_send, adb_ws_recv
from web_.web_shot import web_shot_serve, _find_browser

# Потолок ожидания ответа одного CDP-вызова без своего await, секунды.
WEB_DRIVE_TIMEOUT = 15.0

# Сколько раз и с каким шагом ждать порт DevTools после запуска Chrome.
WEB_DRIVE_PORT_TRIES = 80
WEB_DRIVE_PORT_STEP = 0.25

# Сколько ждать, пока запрошенная страница станет текущим документом.
#
# ⚠ **Ждать обязательно, и вот почему.** Chrome открывает вкладку на `about:blank`
# и только потом идёт по адресу. `/json/list` при этом уже показывает **целевой**
# URL — цель находится, сокет открывается, — а `Runtime.evaluate` попадает ещё в
# контекст `about:blank`. У него опорного адреса нет, и относительный
# `fetch('/auth/login')` падает «Failed to parse URL», а `location.href` отвечает
# `about:blank`. На локальном файле этого не видно: раздача с той же машины
# успевает за миллисекунды, а живой сайт с редиректом — нет.
WEB_DRIVE_LOAD_SEC = 20.0
WEB_DRIVE_LOAD_STEP = 0.2

# Обёртка пользовательского JS: код — тело async-функции; результат — return
# или вызов done(value). Токены подставляются replace'ом: в JS встречается и
# %, и {}, форматирование строкой тут небезопасно.
_WEB_DRIVE_WRAPPER = """
new Promise((resolve) => {
  let settled = false;
  const done = v => { if (!settled) { settled = true; clearTimeout(timer); resolve(v); } };
  const timer = setTimeout(() => {
    if (!settled) { settled = true; resolve({__drv_error: 'done() не вызван, return не выполнен за __WAIT__ мс'}); }
  }, __WAIT__);
  (async () => { __USER__ })().then(v => { if (v !== undefined) done(v); })
    .catch(e => done({__drv_error: String((e && e.stack) || e)}));
})
"""


def web_drive_eval(src, script, wait_ms=5000, storage=(), shot='',
                   size=(1080, 2400), chrome='') -> dict:
    """
    Выполнить JS на загруженной странице headless Chrome; вернуть значение,
    ключи хранилища и всё, что страница наговорила в консоль.

    Args:
        src: путь к локальному HTML (каталог раздаётся HTTP сам, как в
            WebView) или http(s)-URL.
        script: JS — тело async-функции. Результат: `return <значение>` или
            вызов `done(<значение>)`; значение должно сериализоваться в JSON.
            Файловый input накормить можно так:
            `const dt = new DataTransfer(); dt.items.add(new File([blob], 'x.png', {type: 'image/png'})); input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles: true}));`.
        wait_ms: потолок ожидания done(); return значения завершает раньше.
        storage: ключи `localStorage`, которые вернуть после выполнения
            (строка или список имён).
        shot: куда положить PNG страницы после выполнения; пусто — не снимать.
        size: вьюпорт (ширина, высота).
        chrome: путь к бинарю браузера; пусто — поиск как в `web_shot`.

    Returns:
        {'value': результат скрипта, 'console': [{'level','text'}] (падение
        страницы — здесь же с level 'page-error'), 'storage': {ключ: строка
        или None}, 'shot': путь или ''}.

    Raises:
        FileNotFoundError: страницы нет, браузера нет в PATH.
        RuntimeError: Chrome не поднял DevTools, страница не нашлась или
            вызов DevTools не уложился в свои потолки.
    """
    browser = chrome or _find_browser()
    is_url = src.startswith(('http://', 'https://'))
    if not is_url and not os.path.isfile(src):
        raise FileNotFoundError(f'страница не найдена: {src}')

    if is_url:
        return _drive(browser, src, script, wait_ms, storage, shot, size)
    real = os.path.realpath(src)
    with web_shot_serve(os.path.dirname(real) or '.') as root:
        url = f"{root}/{os.path.basename(real)}"
        return _drive(browser, url, script, wait_ms, storage, shot, size)


def _drive(browser, url, script, wait_ms, storage, shot, size) -> dict:
    """Один живой прогон: Chrome с DevTools, evaluate, чтение состояния."""
    profile = tempfile.mkdtemp(prefix='web_drive_')
    proc = subprocess.Popen(
        [browser, '--headless=new', '--disable-gpu', '--hide-scrollbars',
         '--no-first-run', '--no-default-browser-check',
         '--force-device-scale-factor=1',
         f'--user-data-dir={profile}', '--remote-debugging-port=0',
         '--window-size=' + ','.join(str(int(v)) for v in size), url],
        stderr=subprocess.DEVNULL)
    try:
        port = _wait_port(profile)
        sock = adb_ws_open(_page_target(port, url))
        try:
            console: list = []
            _cdp(sock, 1, 'Runtime.enable', {}, console)
            _cdp(sock, 2, 'Log.enable', {}, console)
            _wait_document(sock, url, console)
            js = _WEB_DRIVE_WRAPPER.replace('__USER__', script)
            js = js.replace('__WAIT__', str(int(wait_ms)))
            answer = _cdp(sock, 3, 'Runtime.evaluate',
                          {'expression': js, 'awaitPromise': True,
                           'returnByValue': True}, console,
                          timeout=wait_ms / 1000 + 5)
            details = answer.get('exceptionDetails')
            if details:
                raise RuntimeError('JS обёртки бросил исключение: '
                                   + (details.get('exception', {}).get('description')
                                      or details.get('text', '')))
            storage_map = {}
            for key in ([storage] if isinstance(storage, str) else list(storage)):
                got = _cdp(sock, 4, 'Runtime.evaluate',
                           {'expression': f'localStorage.getItem({json.dumps(key)})',
                            'returnByValue': True}, console)
                storage_map[key] = got.get('result', {}).get('value')
            if shot:
                page = _cdp(sock, 5, 'Page.captureScreenshot', {'format': 'png'}, console)
                with open(shot, 'wb') as fh:
                    fh.write(base64.b64decode(page['data']))
        finally:
            sock.close()
        return {'value': answer.get('result', {}).get('value'),
                'console': console, 'storage': storage_map, 'shot': shot}
    finally:
        _terminate(proc)
        shutil.rmtree(profile, ignore_errors=True)


def _wait_document(sock, url: str, console: list) -> None:
    """Ждёт, пока запрошенная страница станет текущим документом вкладки.

    Chrome открывает вкладку на `about:blank`, и до перехода скрипт выполняется
    там: относительные адреса не разрешаются, `localStorage` принадлежит чужому
    origin, а снимок вышел бы белым. Проверяем **документ**, а не список целей:
    `/json/list` показывает целевой URL раньше, чем тот стал текущим.

    Не дождались — не падаем: страница могла и правда остаться пустой (сайт
    ответил 204, файл не нашёлся). Пусть об этом скажет сам скрипт — его ошибка
    объяснит причину лучше, чем таймаут отсюда.
    """
    deadline = time.monotonic() + WEB_DRIVE_LOAD_SEC
    while time.monotonic() < deadline:
        got = _cdp(sock, 90, 'Runtime.evaluate',
                   {'expression': 'location.href + "|" + document.readyState',
                    'returnByValue': True}, console)
        where, _, state = str(got.get('result', {}).get('value') or '').rpartition('|')
        # ⚠ Сверяем по хвосту адреса, а не целиком: сервер вправе увести
        # редиректом (`/admin` → `/login?next=…`), и это законная загрузка.
        if where and not where.startswith('about:') and state in ('interactive', 'complete'):
            return

        time.sleep(WEB_DRIVE_LOAD_STEP)


def _wait_port(profile: str) -> int:
    """Порт DevTools из файла `DevToolsActivePort` в профиле Chrome."""
    path = os.path.join(profile, 'DevToolsActivePort')
    for _ in range(WEB_DRIVE_PORT_TRIES):
        try:
            with open(path, encoding='utf-8') as fh:
                return int(fh.readline().strip())
        except (FileNotFoundError, ValueError):
            time.sleep(WEB_DRIVE_PORT_STEP)
    raise RuntimeError('Chrome не поднял DevTools-порт — профиль не пишется, '
                       'с запуском браузера что-то не так')


def _page_target(port: int, url: str) -> str:
    """ws-адрес страницы нашего URL; среди лишних about:blank берём её."""
    import urllib.request
    deadline = time.monotonic() + 20
    last_err = 'DevTools молчит'
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=2) as r:
                pages = json.loads(r.read().decode('utf-8'))
            hits = [p for p in pages if p.get('webSocketDebuggerUrl')
                    and p.get('type') == 'page'
                    and url.split('://')[-1] in p.get('url', '')]
            if hits:
                return hits[0]['webSocketDebuggerUrl']
            last_err = f'нет страницы {url!r} среди {len(pages)} целей'
        except OSError as err:
            last_err = str(err)
        time.sleep(0.25)
    raise RuntimeError(f'цель DevTools не найдена: {last_err}')


def _cdp(sock, msg_id: int, method: str, params: dict, console: list,
         timeout: float = WEB_DRIVE_TIMEOUT) -> dict:
    """Один CDP-вызов; события по дороге складываются в `console`.

    От частной `_cdp_call` из `adb_cdp` отличается тем, что не выбрасывает
    поток событий впустую: `Runtime.consoleAPICalled`, `Log.entryAdded` и
    падения страницы приходят между запросами и нужны нам ровно оттуда.
    """
    adb_ws_send(sock, json.dumps({'id': msg_id, 'method': method, 'params': params}))
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise RuntimeError(f'{method}: DevTools не ответил за {timeout:g} с')
        sock.settimeout(min(left, 0.25))
        try:
            answer = json.loads(adb_ws_recv(sock))
        except (TimeoutError, socket.timeout):
            continue
        if answer.get('id') == msg_id:
            if 'error' in answer:
                raise RuntimeError(f"{method}: {answer['error'].get('message', answer['error'])}")
            _drain(sock, console)
            return answer.get('result', {})
        _record(answer, console)


def _drain(sock, console: list) -> None:
    """Выбрать из сокета события, которые уже пришли, и остановиться."""
    sock.settimeout(0.05)
    while True:
        try:
            _record(json.loads(adb_ws_recv(sock)), console)
        except (TimeoutError, socket.timeout):
            return


def _record(answer: dict, console: list) -> None:
    """Событие DevTools → строка консоли; посторонние — мимо."""
    method = answer.get('method', '')
    p = answer.get('params', {})
    if method == 'Runtime.consoleAPICalled':
        parts = [a.get('value', a.get('description', '')) for a in p.get('args', [])]
        console.append({'level': p.get('type', 'log'),
                        'text': ' '.join(str(x) for x in parts).strip()})
    elif method == 'Runtime.exceptionThrown':
        d = p.get('exceptionDetails', {})
        console.append({'level': 'page-error',
                        'text': (d.get('exception', {}).get('description')
                                 or d.get('text', ''))})
    elif method == 'Log.entryAdded':
        e = p.get('entry', {})
        if e.get('level') in ('error', 'warning'):
            console.append({'level': e['level'], 'text': e.get('text', '')})


def _terminate(proc) -> None:
    """Погасить Chrome и дождаться: неубитый headless висит на профиле."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='JS-прогон веб-страницы headless Chrome: значение, '
                    'хранилище, консоль (локальный HTML раздаётся сам).')
    ap.add_argument('src', help='URL или путь к HTML')
    ap.add_argument('--js', default='', help='JS телом: return ... или done(...)')
    ap.add_argument('--js-file', default='', help='то же из файла')
    ap.add_argument('--wait', type=int, default=5000, help='потолок done(), мс')
    ap.add_argument('--storage', default='', help='ключи localStorage через запятую')
    ap.add_argument('--shot', default='', help='PNG страницы после прогона — куда')
    ap.add_argument('--size', default='1080,2400', help='вьюпорт шириной,высотой')
    ap.add_argument('--json', action='store_true', help='ответить одним JSON')
    ns = ap.parse_args()
    script = ns.js or (open(ns.js_file, encoding='utf-8').read() if ns.js_file else '')
    if not script:
        raise SystemExit('нужен --js или --js-file')
    try:
        res = web_drive_eval(ns.src, script, wait_ms=ns.wait,
                             storage=[k for k in ns.storage.split(',') if k],
                             shot=ns.shot,
                             size=tuple(int(v) for v in ns.size.split(',')))
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')
    if ns.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print('значение:', json.dumps(res['value'], ensure_ascii=False))
        for k, v in res['storage'].items():
            print(f'{k}: {v}')
        for line in res['console']:
            print(f"[{line['level']}] {line['text']}")

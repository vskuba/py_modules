"""
Контейнер-браузер из shell: тот же HTTP-API, но без asyncio и без fastapi.

Смежный с `browser_upstream`: адреса, токен и перенос кода ответа — общие
константы того модуля, различие только в транспорте. `browser_upstream` —
асинхронный вход панели, где под рукой httpx и HTTPException; этому нужны
одноразовые прогоны из shell и сюит, где втягивать fastapi ради одного POST
неразумно, а `asyncio.run` ради одного запроса — шум.

Порядок вызова обычный: `browser_http_open` (сессия, опционально со своим
прокси) → `browser_http_goto` → `browser_http_wait`, пока страница не сказала
признаком, который ищем → `browser_http_read` целиком. `browser_http_wait` —
не пауза наугад: страница с JS-рендером (TikTok, поиск картинок) приходит
пустой, и ответ в ней появляется через секунды; ждать признака дольше таймаута
перехода бессмысленно — это та же страница, просто не дорисовалась.

Грабля: сессии живут на контейнере, а не здесь. Сессию, которую завёл, — гаси
(`browser_http_close`), иначе контейнер копит висячие вкладки; у чужих сессий
спрашивать нечего, у своего имени — спрашивать.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from config.config import config_get

# Те же ключи, что у `browser_upstream`, — а не импорт его констант: тот модуль
# втягивает httpx и fastapi, здесь их быть не должно. Источник значения один
# (`.env`), расхождение возможно только если кто-то заведёт третий ключ.
BROWSER_HTTP_URL = config_get('BROWSER_URL', 'http://browser:8000')
BROWSER_HTTP_TOKEN = config_get('BROWSER_API_TOKEN', '')

BROWSER_HTTP_TIMEOUT = 60.0

# Адрес, на котором заведена сессия: `goto`/`read`/`close` должны идти туда же,
# куда и `open`, — иначе переопределение адреса забывается после первого шага.
BROWSER_HTTP_BASES: dict = {}


def browser_http_call(method: str, path: str, payload: dict | None = None,
                      timeout: float = BROWSER_HTTP_TIMEOUT,
                      base: str = BROWSER_HTTP_URL) -> dict:
    """Один запрос к контейнеру; тело — словарь, чужой код ответа — исключение.

    Код >= 400 прилетает `BrowserHttpError` с `detail` контейнера: «нет такой
    сессии» обязано оставаться «нет такой сессии», а не молча возвращаться
    пустым словарём.
    """
    req = urllib.request.Request(base + path,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={'Content-Type': 'application/json',
                                          **({'X-Browser-Token': BROWSER_HTTP_TOKEN} if BROWSER_HTTP_TOKEN else {})},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b'{}').get('detail', '')
        except (ValueError, UnicodeDecodeError):
            detail = ''
        raise BrowserHttpError(e.code, detail or e.reason) from None
    except urllib.error.URLError as e:
        raise BrowserHttpError(0, f'контейнер-браузер недоступен: {e.reason}') from None

    return json.loads(body) if body else {}


class BrowserHttpError(RuntimeError):
    """Контейнер ответил кодом ошибки (code=0 — до него не достучались)."""

    def __init__(self, code: int, detail: str):
        super().__init__(f'[{code}] {detail}')
        self.code = code
        self.detail = detail


def browser_http_open(name: str = 'cli', proxy: str = '', base: str = '') -> str:
    """Завести сессию и вернуть её id; `proxy` — `http://host:port` или `socks5://…`.

    `base` — адрес контейнера, когда `.env` не наш (из-под докера виден
    `http://browser:8000`, с хоста — проброшенный порт).
    """
    payload = {'name': name} | ({'proxy': proxy} if proxy else {})
    url = base or BROWSER_HTTP_URL
    sid = browser_http_call('POST', '/browser/session', payload, base=url)['session']['session_id']
    BROWSER_HTTP_BASES[sid] = url

    return sid


def browser_http_close(sid: str) -> None:
    """Погасить сессию; отсутствующую не считать ошибкой — она могла стухнуть сама."""
    try:
        browser_http_call('DELETE', f'/browser/session/{sid}', base=_base(sid))
    except BrowserHttpError as e:
        if e.code != 404:
            raise
    finally:
        BROWSER_HTTP_BASES.pop(sid, None)


def browser_http_goto(sid: str, url: str, wait_until: str = 'load',
                      timeout_ms: int = 60000) -> dict:
    """Открыть адрес в сессии; `wait_until` — `domcontentloaded` для медленных.

    Для страниц с тяжёлым JS `load` может не наступить никогда (счётчики,
    аналитика держат соединение) — тогда `domcontentloaded` и признак в `wait`.
    """
    return browser_http_call('POST', f'/browser/session/{sid}/goto',
                             {'url': url, 'wait_until': wait_until, 'timeout_ms': timeout_ms},
                             timeout=timeout_ms / 1000 + 30, base=_base(sid))


def browser_http_read(sid: str, selector: str = '') -> dict:
    """Прочитать страницу: `text`, `links` (`[{href, text}]`), `url`."""
    payload = {'selector': selector} if selector else {}
    return browser_http_call('POST', f'/browser/session/{sid}/read', payload, base=_base(sid))


def browser_http_wait(sid: str, contains: str, tries: int = 10,
                      delay: float = 2.0) -> dict:
    """Читать, пока в тексте не появится `contains`; истощился — последний снимок.

    Возвращает как `browser_http_read`; признака нет — тот же словарь, просто
    `contains` в его `text` отсутствует: проверяющему решать, пустая страница
    это или ещё не дорисовалась.
    """
    page = {}
    for _ in range(tries):
        page = browser_http_read(sid)
        if contains in (page.get('text') or ''):
            return page
        time.sleep(delay)

    return page


def _base(sid: str) -> str:
    return BROWSER_HTTP_BASES.get(sid, BROWSER_HTTP_URL)


def browser_http_capture(sid: str, out: str) -> str:
    """Кадр окна в файл (base64 из контейнера ложится на диск как есть)."""
    shot = browser_http_call('POST', f'/browser/session/{sid}/capture', {},
                            timeout=90, base=_base(sid))
    Path(out).write_bytes(base64.b64decode(shot['image'].split(',', 1)[-1]))
    return out

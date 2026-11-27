"""
Карточка HTTP-вызова «запрос → ответ» картинкой PNG — для тикета или отчёта.

Родился на задаче «покажи в Jira, что API работает»: скриншот Postman есть не
у всех, а сырой JSON в комментарии никто не читает. Карточка — метод и адрес,
заголовок авторизации (замазан), тело запроса слева, код и тело ответа справа,
подпись снизу. Рендер — headless Chromium через Playwright, без сервера.

## ⚠ Секреты

Ключи из `HTTP_SHOT_MASK` (без учёта регистра, на любой глубине) заменяются на
`••••••`: пароли, токены, секреты, CVV. Номер карты сам по себе **не**
маскируется — у платёжных тестов это публичная тестовая карта, и её видно
нарочно; боевые номера в карточку не класть. Добавить ключи — `--mask`.

Длинные массивы и словари в ответе сворачиваются по `--collapse КЛЮЧ` в `[…]` /
`{…}` — экран важнее полноты; полный ответ — в файле, не в картинке.

CLI (тела — файлы JSON; у ответа можно `{"status": 200, "body": {...}}`):

    python -m http_.http_shot --title 'Payout methods' --method POST \\
        --url https://api.example.com/v1/payout/methods \\
        --request req.json --response resp.json --note 'подпись' \\
        --collapse apms --out shot.png

Метки по JSON-пути (`$.fiat_data.amount`) и spec-файл целиком — см.
`http_card` (`http_/http_card.py`): это та же карточка для отчётов, где важны
рамки на конкретных полях.
"""
import argparse
import html
import json

HTTP_SHOT_MASK = {'password', 'passwd', 'secret', 'token', 'access_token', 'refresh_token',
                  'api_key', 'apikey', 'authorization', 'cvv', 'cvc', 'checksum',
                  # ключи подписи и шифрования из callback-контуров: подпись
                  # подделывают по значению, в кадр ей делать нечего
                  'x-api-key', 'api_token', 'encryption_key', 'signature'}
HTTP_SHOT_WIDTH = 1548

_CSS = """
body{margin:0;background:#f3f4f6;font:14px -apple-system,Segoe UI,Roboto,sans-serif;color:#111827}
.wrap{padding:24px} h1{font-size:20px;margin:0 0 12px}
.bar{display:flex;gap:10px;align-items:center;background:#fff;border:1px solid #d1d5db;
 border-radius:8px;padding:10px 14px;margin-bottom:14px}
.m{background:#f59e0b;color:#fff;font-weight:700;border-radius:4px;padding:2px 8px}
.u{font-family:ui-monospace,Menlo,monospace}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:#fff;border:1px solid #d1d5db;border-radius:8px;overflow:hidden}
.h{background:#e5e7eb;padding:8px 12px;font-weight:600;display:flex;justify-content:space-between}
pre{margin:0;padding:12px;font:12.5px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;
 word-break:break-all;background:#0f172a;color:#e2e8f0}
.ok{color:#16a34a} .bad{color:#dc2626} .note{margin-top:12px;color:#4b5563}
"""


def http_shot_mask(data, keys: set = HTTP_SHOT_MASK, collapse: set | None = None):
    """Замазать секреты (пароль, токен, CVV) в JSON запроса/ответа перед скриншотом; свернуть `collapse`."""
    collapse = collapse or set()
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if str(k).lower() in keys and v not in (None, ''):
                out[k] = '••••••'
            elif k in collapse:
                out[k] = '[…]' if isinstance(v, list) else '{…}' if isinstance(v, dict) else v
            else:
                out[k] = http_shot_mask(v, keys, collapse)
        return out
    if isinstance(data, list):
        return [http_shot_mask(v, keys, collapse) for v in data]
    return data


def http_shot_html(title: str, method: str, url: str, request, status, response,
                   note: str = '', auth: str = 'Bearer') -> str:
    """HTML карточки «запрос и ответ API» для скриншота; тела уже замазаны (`http_shot_mask`)."""
    dump = lambda o: html.escape(json.dumps(o, indent=2, ensure_ascii=False)) if o is not None else '—'
    cls = 'ok' if str(status).startswith('2') else 'bad'
    auth_line = f'Authorization: {html.escape(auth)} •••••••• · ' if auth else ''
    return (f'<!doctype html><html><head><meta charset=utf-8><style>{_CSS}</style></head><body>'
            f'<div class=wrap><h1>{html.escape(title)}</h1>'
            f'<div class=bar><span class=m>{html.escape(method)}</span><span class=u>{html.escape(url)}</span></div>'
            f'<div class=bar><span class=u>{auth_line}Content-Type: application/json</span></div>'
            f'<div class=cols><div class=card><div class=h>Request body</div><pre>{dump(request)}</pre></div>'
            f'<div class=card><div class=h>Response <span class={cls}>{html.escape(str(status))}</span></div>'
            f'<pre>{dump(response)}</pre></div></div>'
            f'<div class=note>{html.escape(note)}</div></div></body></html>')


def http_shot_render(page_html: str, out: str, width: int = HTTP_SHOT_WIDTH) -> str:
    """Скриншот карточки запроса и ответа API в PNG для тикета (Playwright); вернуть путь."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 800})
        page.set_content(page_html)
        page.screenshot(path=out, full_page=True)
        browser.close()
    return out


def _load(path: str):
    if not path:
        return None, None
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, dict) and set(data) == {'status', 'body'}:
        return data['status'], data['body']
    return None, data


def _cli(argv=None):
    ap = argparse.ArgumentParser(description='Карточка HTTP-вызова «запрос → ответ» в PNG.')
    ap.add_argument('--title', required=True)
    ap.add_argument('--method', default='POST')
    ap.add_argument('--url', required=True)
    ap.add_argument('--request', default='', help='JSON тела запроса')
    ap.add_argument('--response', required=True, help='JSON ответа или {"status":…, "body":…}')
    ap.add_argument('--status', default='', help='код ответа, если его нет в файле')
    ap.add_argument('--note', default='')
    ap.add_argument('--auth', default='Bearer', help="схема в строке Authorization; '' — не показывать")
    ap.add_argument('--mask', action='append', default=[], help='ещё ключ для замазывания')
    ap.add_argument('--collapse', action='append', default=[], help='ключ, который свернуть в […]/{…}')
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)

    keys = HTTP_SHOT_MASK | {k.lower() for k in a.mask}
    _, request = _load(a.request)
    status, response = _load(a.response)
    status = a.status or status or 200
    page_html = http_shot_html(a.title, a.method, a.url, http_shot_mask(request, keys),
                               status, http_shot_mask(response, keys, set(a.collapse)), a.note, a.auth)
    print(http_shot_render(page_html, a.out))


if __name__ == '__main__':
    _cli()

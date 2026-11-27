"""
Карточка HTTP-вызова «запрос → ответ» по spec-файлу: с метками по JSON-пути.

От `http_shot` отличается входом и метками: вместо россыпи флагов — один
JSON-файл (`--spec`), и каждое важное поле обводится красной рамкой со
стрелкой: метка задаётся JSON-путём (`$.fiat_data.amount`,
`response.$.description`, `headers.$.Authorization`, `url.$.signature`), а не
координатами. Родилось из сборки PDF-отчётов stage-тестов, где карточка
собиралась heredoc'ом на каждую из десяти, а метки вешались zip'ом по порядку
DOM — при двух метках в одном теле стрелка уезжала не туда. Здесь метка —
якорь `data-m` на своей строке, порядок DOM в расчёт не берётся вовсе.

Секреты (пароли, токены, ключи, подписи, PAN) замазываются до рендера:
имена ключей сравниваются без учёта регистра (`API_TOKEN` из настроек — это
тот же `api_token`; из-за пренебрега утёк тестовый ключ), значения чинятся в
теле, в заголовках и в строке адреса (`signature=` внутри
`continue_request.uri` — тоже подпись). Номер карты — последние 4 символа.
Страховка — `data_.data_mask.data_mask_sweep`: по списку запрещённых значений
(`--forbid`) собранный HTML проверяется до записи файла; утечка — исключение,
файл не пишется.

CLI (spec — JSON; значения forbid — файлом-списком: JSON-массив или построчно):

    python -m http_.http_card --spec card.json --out card.png [--forbid secrets.txt]

Spec:

    {"title": "Refund 10.00", "method": "POST", "url": "https://api.../?signature=...",
     "request_headers": {"Authorization": "..."}, "request_body": {...} | "text=...",
     "status": 200, "response_body": {...} | "raw text", "caption": "...",
     "marks": [{"path": "$.fiat_data.amount", "label": "amount in cents"}]}

Метки без стороны ищутся сначала в ответе, потом в запросе; у тела-текста
`$.key` ловит строку с `key=`.
"""
import argparse
import html as _html_mod
import json
import re

from data_.data_mask import (DATA_MASK_PAN_RE, DATA_MASK_SECRET_KEYS,
                             data_mask_sweep_text, data_mask_text, data_mask_values)
from http_.http_shot import HTTP_SHOT_MASK

HTTP_CARD_WIDTH = 1548
HTTP_CARD_DOTS = '•' * 6
# Имена секретных ключей: список `http_shot` плюс ключи подписи и шифрования из
# `data_mask`; сравнение — по lower(), UPPER-ключи настроек попадают сюда же.
HTTP_CARD_SECRET_KEYS = frozenset(
    {k.lower() for k in HTTP_SHOT_MASK} |
    {k.lower() for k in DATA_MASK_SECRET_KEYS} |
    {'x-api-key', 'api_token', 'encryption_key', 'signature'})

_CARD_SIDES = ('request', 'response', 'headers', 'url')
_CARD_QUERY_KV = re.compile(r'([?&"])([A-Za-z0-9_.\-]+)(=)([^&"\\\s]*)')
_CARD_PAN_KEEP = 4  # «card number → last4»: маскируется всё, кроме хвоста
_CARD_STEP = re.compile(r'\.([^.\[\]]+)|\[(\d+)\]')

_CARD_CSS = """
body{margin:0;background:#f3f4f6;font:14px -apple-system,Segoe UI,Roboto,sans-serif;color:#111827}
.wrap{padding:24px} h1{font-size:20px;margin:0 0 12px}
.bar{display:flex;gap:10px;align-items:center;background:#fff;border:1px solid #d1d5db;
 border-radius:8px;padding:10px 14px;margin-bottom:10px}
.m{background:#f59e0b;color:#fff;font-weight:700;border-radius:4px;padding:2px 8px}
.u{font-family:ui-monospace,Menlo,monospace;word-break:break-all}
.hdr{background:#fff;border:1px solid #d1d5db;border-radius:8px;padding:8px 14px;margin-bottom:14px}
.hdr div{font:12.5px/1.6 ui-monospace,Menlo,monospace;word-break:break-all}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
.card{background:#fff;border:1px solid #d1d5db;border-radius:8px;overflow:hidden}
.hd{background:#e5e7eb;padding:8px 12px;font-weight:600;display:flex;justify-content:space-between}
pre{margin:0;padding:12px;font:12.5px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;
 word-break:break-all;background:#0f172a;color:#e2e8f0}
.ok{color:#16a34a} .bad{color:#dc2626} .note{margin-top:12px;color:#4b5563}
"""


def http_card_render(spec: dict, out: str, width: int = HTTP_CARD_WIDTH,
                     forbid=(), mask=()) -> dict:
    """Карточка «запрос → ответ» по spec в PNG с метками по JSON-пути; вернуть {'out','marks'}.

    Секреты замазаны до рендера; с `forbid` (список реальных значений) собранный
    HTML прогоняется через `data_mask_sweep_text` — утечка поднимает RuntimeError,
    файл не пишется. Метки ищутся по `data-m`-якорям, а не порядком DOM; рамку,
    стрелку и плашку рисует `image_.image_arrow.image_arrows`. Высоту кадра
    меряет Playwright (`full_page`), задавать её не нужно.

    Args:
        spec: см. докстринг модуля; неизвестные поля игнорируются.
        out: путь PNG.
        width: ширина вьюпорта в CSS-пикселях.
        forbid: значения, которым запрещено появиться на картинке.
        mask: дополнительные имена ключей для замазывания.
    """
    page_html = http_card_html(spec, mask=mask)
    if forbid:
        _sweep_html(page_html, forbid)
    placed = [{'path': m.get('path', ''), 'label': m.get('label', ''), 'found': False}
              for m in (spec.get('marks') or [])]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 800})
        page.set_content(page_html)
        boxes = []
        for i, one in enumerate(placed):
            loc = page.locator(f'[data-m="{i}"]')
            if loc.count() == 0:
                continue
            box = loc.first.bounding_box()
            if not box:
                continue
            one['found'] = True
            boxes.append({'box': (int(box['x']), int(box['y']),
                                  int(box['width']), int(box['height'])),
                          'label': one['label']})
        page.screenshot(path=out, full_page=True)
        browser.close()
    if boxes:
        from image_.image_arrow import image_arrows
        image_arrows(out, boxes)
    return {'out': out, 'marks': placed}


def http_card_html(spec: dict, mask=()) -> str:
    """HTML карточки по spec: секретов нет, метки стоят якорями `data-m` по JSON-пути.

    Якорь — `<span data-m="номер">` на найденной строке: две метки в одном теле
    не путаются, потому что номер метки — часть разметки, а не позиция в DOM.
    """
    keys = HTTP_CARD_SECRET_KEYS | {k.lower() for k in mask}
    marks = [_parse_path(m.get('path', ''), i)
            for i, m in enumerate(spec.get('marks') or [])]
    url_masked = _mask_url(spec.get('url', ''), keys)
    headers = {k: _mask_value(v, keys) if not _is_secret(k, keys) else HTTP_CARD_DOTS
               for k, v in (spec.get('request_headers') or {}).items()}
    head_lines = [(f'{_esc(k)}: {_esc(v)}', (str(k).lower(),)) for k, v in headers.items()]
    req_lines = _emit(_mask_value(spec.get('request_body'), keys))
    rsp_lines = _emit(_mask_value(spec.get('response_body'), keys))
    anchors = {}  # номер метки → ('line', документ, строка) | ('range', текст, start, end)
    for side, tokens, number in marks:
        if side == 'url':
            anchors[number] = ('range', url_masked) + _find_query(url_masked, tokens)
        elif side == 'headers':
            anchors[number] = ('line', 'headers', _find_line(head_lines, tokens, lower=True))
        elif side == 'request':
            anchors[number] = ('line', 'request', _find_line(req_lines, tokens))
        elif side == 'response':
            anchors[number] = ('line', 'response', _find_line(rsp_lines, tokens))
        else:  # без стороны: сначала ответ, потом запрос
            found = _find_line(rsp_lines, tokens)
            anchors[number] = (('line', 'response', found) if found is not None
                               else ('line', 'request', _find_line(req_lines, tokens)))
    pre = {'headers': _join_lines(head_lines, anchors, 'headers', sep='<br>'),
           'request': _pre(req_lines, anchors, 'request'),
           'response': _pre(rsp_lines, anchors, 'response')}
    url_body = _span_range(url_masked, anchors)
    status = spec.get('status', 200)
    cls = 'ok' if str(status).startswith('2') else 'bad'
    return (f'<!doctype html><html><head><meta charset=utf-8><style>{_CARD_CSS}</style>'
            f'</head><body><div class=wrap><h1>{_esc(spec.get("title", ""))}</h1>'
            f'<div class=bar><span class=m>{_esc(spec.get("method", "POST"))}</span>'
            f'<span class=u>{url_body}</span></div>'
            f'<div class=hdr><div>{pre["headers"]}</div></div>'
            f'<div class=cols><div class=card><div class=hd>Request body</div>{pre["request"]}</div>'
            f'<div class=card><div class=hd>Response <span class={cls}>{_esc(str(status))}</span>'
            f'</div>{pre["response"]}</div></div>'
            f'<div class=note>{_esc(spec.get("caption", ""))}</div></div></body></html>')


def http_card_mask(value, mask=()):
    """Значение (dict/list/str) без секретов: ключ — без учёта регистра, query и PAN — в строках."""
    return _mask_value(value, HTTP_CARD_SECRET_KEYS | {k.lower() for k in mask})


# ── приватное ──


def _esc(text) -> str:
    return _html_mod.escape(str(text), quote=False)


def _is_secret(key, keys: frozenset) -> bool:
    return str(key).lower().replace('-', '_') in keys


def _mask_text(text: str, keys: frozenset) -> str:
    """Строка без секретов: `key=` в query, PAN (last4), токены вида Bearer/JWT."""

    def kv(match):
        key = match.group(2).lower().replace('-', '_')
        if key not in keys:
            return match.group(0)
        return f'{match.group(1)}{match.group(2)}{match.group(3)}' + HTTP_CARD_DOTS

    text = _CARD_QUERY_KV.sub(kv, text)
    text = DATA_MASK_PAN_RE.sub(
        lambda m: '•' * (len(m.group(0)) - _CARD_PAN_KEEP) + m.group(0)[-_CARD_PAN_KEEP:],
        text)
    masked, err = data_mask_text(text)
    return text if err else masked


def _mask_value(node, keys: frozenset):
    if isinstance(node, dict):
        return {k: (HTTP_CARD_DOTS if _is_secret(k, keys) and v not in (None, '')
                    else _mask_value(v, keys)) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_value(v, keys) for v in node]
    if isinstance(node, str):
        return _mask_text(node, keys)
    return node


def _mask_url(url: str, keys: frozenset) -> str:
    """URL без секретов: `key=` из query чинится так же, как в теле."""
    return _mask_text(url, keys)


def _find_query(text: str, tokens: tuple) -> tuple:
    """Где в URL лежит `param=`: (start, end) или (None, None)."""
    if not tokens:
        return None, None
    match = re.search(rf'[?&]{re.escape(str(tokens[0]))}=[^&\\\s"]*', text)
    return (match.start(), match.end()) if match else (None, None)


def _parse_path(path: str, number: int) -> tuple:
    """`response.$.a[0].b` → ('response', ('a', 0, 'b'), 0); голый `$.…` → ('', …)."""
    rest = str(path).strip()
    side = ''
    for prefix in _CARD_SIDES:
        if rest.startswith(prefix + '.'):
            side, rest = prefix, rest[len(prefix) + 1:]
            break
    if side in ('headers', 'url'):
        tokens = (rest.lstrip('$').lstrip('.').strip('[]"\''),)
        return side, tokens, number
    if rest.startswith('$'):
        rest = rest[1:]
    if rest and not rest.startswith(('.', '[')):
        rest = '.' + rest
    tokens = tuple(int(step[1]) if step[1] else step[0]
                   for step in _CARD_STEP.findall(rest))
    return side, tokens, number


def _emit(node, tokens=(), indent=0, out=None, opening=True) -> list:
    """Pretty-JSON построчно: [(текст, tokens)] — tokens это чей путь описывает строка."""
    if out is None:
        out = []
    pad = '  ' * indent
    if isinstance(node, dict) and node:
        if opening:
            out.append((pad + '{', tokens))
        items = list(node.items())
        for pos, (key, value) in enumerate(items):
            own = tokens + (key,)
            if isinstance(value, (dict, list)) and value:
                head = '[' if isinstance(value, list) else '{'
                out.append((f'{pad}  {_esc(json.dumps(str(key), ensure_ascii=False))}: {head}', own))
                _emit(value, own, indent + 1, out, opening=False)
            else:
                out.append((f'{pad}  {_esc(json.dumps(str(key), ensure_ascii=False))}: '
                            f'{_esc(json.dumps(value, ensure_ascii=False))}', own))
            if pos != len(items) - 1:  # запятая дописывается в последнюю строку элемента
                out[-1] = (out[-1][0] + ',', own)
        out.append((pad + '}', tokens))
        return out
    if isinstance(node, list) and node:
        if opening:
            out.append((pad + '[', tokens))
        for pos, value in enumerate(node):
            own = tokens + (pos,)
            if isinstance(value, (dict, list)) and value:
                head = '[' if isinstance(value, list) else '{'
                out.append((f'{pad}  {head}', own))
                _emit(value, own, indent + 1, out, opening=False)
            else:
                out.append((f'{pad}  {_esc(json.dumps(value, ensure_ascii=False))}', own))
            if pos != len(node) - 1:
                out[-1] = (out[-1][0] + ',', own)
        out.append((pad + ']', tokens))
        return out
    if node is None:
        out.append((pad + 'null', tokens))
    elif isinstance(node, str) and '\n' in node and not tokens:
        out.extend((_esc(line), ()) for line in node.splitlines())  # тело-текст
    else:
        out.append((pad + _esc(json.dumps(node, ensure_ascii=False)), tokens))
    return out


def _find_line(lines: list, tokens: tuple, lower: bool = False):
    """Первая строка, которую описывает путь: точный путь, имя заголовка (регистр — не аргумент) или `key=` в теле-тексте."""
    for i, (text, line_tokens) in enumerate(lines):
        if line_tokens == tokens:
            return i
        if len(tokens) == 1 and lower and len(line_tokens) == 1 and \
                str(line_tokens[0]).lower() == str(tokens[0]).lower():
            return i
        if len(tokens) == 1 and not line_tokens and re.search(
                rf'(^|[&?"\s]){re.escape(str(tokens[0]))}["\s]*[=:]', text):
            return i
    return None


def _anchor(number: int, content: str) -> str:
    return f'<span data-m="{number}">{content}</span>'


def _join_lines(lines: list, anchors: dict, doc: str, sep: str = '\n') -> str:
    """Строки документа в markup; строка с якорем метки оборачивается span'ом."""
    by_line = {a[2]: number for number, a in anchors.items()
               if a[0] == 'line' and a[1] == doc and a[2] is not None}
    parts = []
    for i, (text, _) in enumerate(lines):
        parts.append(_anchor(by_line[i], text) if i in by_line else text)
    return sep.join(parts)


def _pre(lines: list, anchors: dict, doc: str) -> str:
    return f'<pre>{_join_lines(lines, anchors, doc)}</pre>'


def _span_range(text: str, anchors: dict) -> str:
    """URL целиком: якоря — spans поверх найденных `param=` (границы по сырой строке)."""
    spans = sorted(((a[2], a[3], n) for n, a in anchors.items()
                    if a[0] == 'range' and a[2] is not None))
    out, cursor = '', 0
    for start, end, number in spans:
        out += _esc(text[cursor:start]) + _anchor(number, _esc(text[start:end]))
        cursor = end
    return out + _esc(text[cursor:])


def _sweep_html(page_html: str, forbid) -> None:
    """В собранном HTML нет ни одного запрещённого значения, иначе — Leak наружу."""
    hits, err = data_mask_sweep_text(page_html, forbid)
    if err:
        raise RuntimeError(f'forbid: {err}')
    if hits:
        where = ', '.join(f'{h["value"]}… x{h["count"]}' for h in hits)
        raise RuntimeError(f'в карточке остались запрещённые значения: {where}')


def _cli(argv=None):
    ap = argparse.ArgumentParser(prog='python -m http_.http_card',
                                 description='карточка запрос→ответ по spec, метки по JSON-пути')
    ap.add_argument('--spec', required=True, help='JSON-файл со spec (см. докстринг модуля)')
    ap.add_argument('--out', required=True, help='куда лечь PNG')
    ap.add_argument('--width', type=int, default=HTTP_CARD_WIDTH)
    ap.add_argument('--mask', action='append', default=[], help='ещё имя ключа для замазывания')
    ap.add_argument('--forbid', default='', metavar='FILE',
                    help='список значений (JSON-массив или построчно), которым нельзя быть в PNG')
    ns = ap.parse_args(argv)
    with open(ns.spec, encoding='utf-8') as fh:
        spec = json.load(fh)
    forbid = ()
    if ns.forbid:
        forbid, err = data_mask_values(ns.forbid)
        if err:
            raise SystemExit(f'ошибка: forbid: {err}')
    got = http_card_render(spec, ns.out, width=ns.width, mask=ns.mask, forbid=forbid)
    print(json.dumps(got, ensure_ascii=False))
    missing = [m['path'] for m in got['marks'] if not m['found']]
    if missing:  # молча нарисованная карточка без метки — хуже ошибки
        print(f'метки не найдены в телах: {missing}')
        raise SystemExit(2)


if __name__ == '__main__':
    _cli()

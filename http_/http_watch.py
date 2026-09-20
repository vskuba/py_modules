"""Сторож JSON-эндпоинта: текущее значение поля и готовая проба для watch'а.

`state_watch` стережёт колонку базы, `web_watch` — страницу; когда же объект
наблюдения — чужой API (статус интента, число входящих сделок), цикл «полить
URL — извлечь путь — показать было→стало» собирался заново в каждом проекте.
Здесь он один: значение извлекается точкой пути (`status`, `data.intent.status`),
проба отдаётся строкой, исполнимой сторожем как есть.
"""

import os
import urllib.request
import urllib.error


def http_watch(url: str, field: str | None = None,
               headers: dict | None = None) -> dict:
    """Значение поля JSON-ответа сейчас + готовая строка-проба часовому сторожу.

    Args:
        url: JSON-эндпоинт, который сторожат.
        field: точка пути (`status`, `data.intent.status`); пусто — весь ответ.
        headers: заголовки (например `{'x-api-key': …}`).

    Returns:
        {'value_of': значение (None — поля нет), 'probe': строка команды для
        сторожа: `python -m http_.http_watch <url> --field путь`}.
    """
    req = urllib.request.Request(url, headers={'accept': 'application/json',
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            import json
            j = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {'value_of': None, 'probe': _probe(url, field),
                'error': f'HTTP {e.code}'}
    v = j
    for p in (field.split('.') if field else []):
        if isinstance(v, dict) and p in v:
            v = v[p]
        else:
            v = None
            break
    return {'value_of': v, 'probe': _probe(url, field)}


# ─── приватное ─────────────────────────────────────────────────────────────

def _probe(url: str, field: str | None) -> str:
    return f'python -m http_.http_watch {url}' + (f' --field {field}' if field else '') + ' --format value'


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='значение поля JSON-эндпоинта сейчас'
                                             ' + проба сторожу')
    ap.add_argument('url')
    ap.add_argument('--field', help='точка пути в JSON')
    ap.add_argument('--header', action='append', default=[], metavar='КЛЮЧ=значение')
    ap.add_argument('--format', default='json', choices=['json', 'value'])
    ns = ap.parse_args()
    hs = dict(h.split('=', 1) for h in ns.header)
    out = http_watch(ns.url, ns.field, hs)
    print(out['value_of'] if ns.format == 'value' else out['value_of'])

"""Сторож JSON-эндпоинта: текущее значение поля и готовая проба для watch'а.

`state_watch` стережёт колонку базы, `web_watch` — страницу; когда же объект
наблюдения — чужой API (статус интента, число входящих сделок), цикл «полить
URL — извлечь путь — показать было→стало» собирался заново в каждом проекте.
Здесь он один: значение извлекается точкой пути (`status`, `data.intent.status`),
проба отдаётся строкой, исполнимой сторожем как есть.
"""

import json
import urllib.error
import urllib.request

# Потолок на один поход к чужому API. Сторож зовут по кругу, и повисший
# запрос останавливает не один вызов, а весь круг.
HTTP_WATCH_TIMEOUT = 15


def http_watch(url: str, field: str | None = None,
               headers: dict | None = None,
               timeout: float = HTTP_WATCH_TIMEOUT) -> dict:
    """Значение поля JSON-ответа сейчас + готовая строка-проба часовому сторожу.

    Args:
        url: JSON-эндпоинт, который сторожат.
        field: точка пути (`status`, `data.intent.status`); индекс списка —
            числом (`items.0.state`); пусто — весь ответ.
        headers: заголовки (например `{'x-api-key': …}`).
        timeout: секунды на запрос.

    Returns:
        {'value_of': значение (None — поля нет), 'probe': строка команды для
        сторожа: `python -m http_.http_watch <url> --field путь`}; при отказе
        добавляется 'error' словами, а `value_of` остаётся None.

    ⚠ Отказ — часть ответа, а не исключение: сторож зовут по кругу, и упавший
    трейсбеком круг обрывается. Сеть, таймаут, не-JSON в теле и отсутствие
    поля выглядят одинаково — `value_of: None` плюс 'error'.
    """
    req = urllib.request.Request(url, headers={'accept': 'application/json',
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {'value_of': None, 'probe': _probe(url, field),
                'error': f'HTTP {e.code}'}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {'value_of': None, 'probe': _probe(url, field),
                'error': f'не дозвонились: {e}'}
    except ValueError as e:
        return {'value_of': None, 'probe': _probe(url, field),
                'error': f'ответ не JSON: {e}'}
    return {'value_of': _dig(body, field), 'probe': _probe(url, field)}


# ─── приватное ─────────────────────────────────────────────────────────────

def _dig(body, field: str | None):
    """Значение по точке пути; целое колено — индекс списка (`items.0.state`)."""
    value = body
    for part in (field.split('.') if field else []):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.lstrip('-').isdigit():
            index = int(part)
            if not -len(value) <= index < len(value):
                return None
            value = value[index]
        else:
            return None
    return value


def _probe(url: str, field: str | None) -> str:
    return (f'python -m http_.http_watch {url}'
            + (f' --field {field}' if field else '') + ' --format value')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='значение поля JSON-эндпоинта сейчас'
                                             ' + проба сторожу')
    ap.add_argument('url')
    ap.add_argument('--field', help='точка пути в JSON')
    ap.add_argument('--header', action='append', default=[], metavar='КЛЮЧ=значение')
    ap.add_argument('--timeout', type=float, default=HTTP_WATCH_TIMEOUT)
    ap.add_argument('--expect', default=None,
                    help='ждём это значение; код возврата 0 — совпало, 1 — нет')
    ap.add_argument('--format', default='json', choices=['json', 'value'])
    ns = ap.parse_args()
    out = http_watch(ns.url, ns.field, dict(h.split('=', 1) for h in ns.header),
                     timeout=ns.timeout)
    # `--format json` печатает JSON целиком (с 'probe' и 'error'), `value` —
    # одно значение: раньше обе ветки печатали одно и то же, и пробу сторожу
    # приходилось собирать руками.
    print(json.dumps(out, ensure_ascii=False, default=str) if ns.format == 'json'
          else out['value_of'])
    if ns.expect is not None:
        raise SystemExit(0 if str(out['value_of']) == ns.expect else 1)
    raise SystemExit(1 if out.get('error') else 0)

"""JS страницы как код: парсится ли он так, как его читает браузер (ESM), и где ломается.

Ближайший сосед — `file_check`, но он про байты (пустышка, огрызок, голова), а этот
модуль — про рантайм браузера. Смысл отдельного инструмента в том, что `node --check`
по `.js` молчит там, где браузер уже умер: `.js` читается CommonJS-парсером и до
доехавшей из шаблона многострочной строки не доходит — страница гибнет с
«Invalid or unexpected token», а стек пуст, потому что модуль не исполнился вовсе.
Здесь код парсится ровно так, как его парсит браузер, и наружу выходят адрес
поломки (с куском исходника) и граф импортов — кто кого подключает.

Инлайн-модуль шаблона — тот же код, что и внешний `.js`, поэтому и он парсится
здесь: блоки `<script type="module">` вынимаются из HTML с их же номерами строк.
"""
import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Потолок одного парсинга, секунды: зависший node не должен держать вызывающего.
JS_CHECK_TIMEOUT = 10.0

# Инлайн-модуль шаблона: кавычки атрибута любые (как в `web_drive_page`).
JS_CHECK_MODULE = re.compile(
    r'<script[^>]*\btype\s*=\s*(["\'])module\1[^>]*>(.*?)</script>', re.I | re.S)
# Импорт: с «from» и без (побочный импорт — тоже связь графа). Кавышки внешние,
# внутренние не режем: в шаблонах путь живёт и внутри `{{ static_url('…') }}`.
JS_CHECK_IMPORT = re.compile(
    r'import\s+(?:[\s\S]*?\sfrom\s*)?(["\'])((?:(?!\1)[\s\S])+?)\1')

# stderr node: `<файл>:<номер>` — сама строка — каретка — `SyntaxError: <что>`.
JS_CHECK_AT = re.compile(r'^(\S+):(\d+)$', re.M)
JS_CHECK_ERR = re.compile(r'^(SyntaxError|TypeError|ReferenceError): (.*)$', re.M)


def js_check(path) -> dict:
    """Распарсится ли JS как браузерный ESM-модуль; где поломка и кто кого импортирует.

    `.js`/`.mjs` парсятся целиком; из `.html`/`.htm` вынимаются блоки
    `<script type="module">` и парсится тело каждого — с исходным номером строки
    шаблона. Классический `<script>` без `type=module` — не модуль, он не в графе.

    Args:
        path: путь к `.js`/`.mjs` или к странице `.html`/`.htm`.

    Returns:
        {'цел': bool, 'что': 'SyntaxError: …' или '', 'где': 'файл:строка',
         'кусок': строка исходника в месте поломки, 'импортируют': [адреса]}:
         у целого файла 'что'/'где'/'кусок' пустые; 'импортируют' — статический
         граф (куда смотрят импорты этого файла), пуст у страницы без модуля.

    ⚠ node — системная зависимость (как ffmpeg в `adb_rec`): нет бинаря —
    отдельная ошибка `FileNotFoundError`, а не молчаливый пропуск.
    """
    path = Path(path)
    code = path.read_text(encoding='utf-8', errors='replace')
    if path.suffix.lower() in ('.html', '.htm'):
        blocks = [(1 + code.count('\n', 0, m.start(2)), m.group(2))
                  for m in JS_CHECK_MODULE.finditer(code) if m.group(2).strip()]
    elif path.suffix.lower() in ('.js', '.mjs'):
        blocks = [(1, code)]
    else:
        raise ValueError(f'не код: {path.name} (ожидается .js/.mjs или .html)')

    edges, found = [], []
    for line0, body in blocks:
        edges += [m.group(2) for m in JS_CHECK_IMPORT.finditer(body)]
        bad = _check(body, line0, str(path))
        if bad:
            found.append(bad)
    return {'цел': not found,
            'что': found[0]['что'] if found else '',
            'где': found[0]['где'] if found else '',
            'кусок': found[0]['кусок'] if found else '',
            'импортируют': sorted(set(edges))}


# ── детали реализации ──

def _check(body: str, line0: int, where: str) -> dict:
    """Один блок кода через node --check как .mjs: {} когда цел, иначе находка.

    Тело пишется во временный `.mjs` с отступом строк до `line0` — чтобы номер в
    stderr node был родным для шаблона, без пересчёта на месте: кусок берётся из
    исходного файла по этому же номеру.
    """
    node = shutil.which('node')
    if not node:
        raise FileNotFoundError('node не найден в PATH — проверять целость кода '
                                'нечем (зависимость системная, как ffmpeg в adb_rec)')
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                     encoding='utf-8') as fh:
        fh.write('\n' * (line0 - 1) + body)
        tmp = fh.name
    try:
        done = subprocess.run([node, '--check', tmp], capture_output=True,
                              text=True, timeout=JS_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'node не завершился за {JS_CHECK_TIMEOUT:g} с: {where}')
    finally:
        Path(tmp).unlink(missing_ok=True)
    if done.returncode == 0:
        return {}
    err = done.stderr
    at, errm = JS_CHECK_AT.search(err), JS_CHECK_ERR.search(err)
    number = int(at.group(2)) if at else 0
    lines = body.splitlines()
    i = number - line0  # temp-файл дополнен строк до line0, строка тела сдвинута
    return {'что': errm.group(0) if errm else err.strip()[:200],
            'где': (f'{where}:{number}' if at else where),
            'кусок': lines[i].strip() if 0 <= i < len(lines) else ''}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Цел ли JS как браузерный модуль (ESM): парсинг node, адрес '
                    'поломки с куском исходника, граф импортов.')
    ap.add_argument('files', nargs='+', help='файлы .js/.mjs/.html')
    ns = ap.parse_args()
    broken = 0
    for f in ns.files:
        try:
            r = js_check(f)
        except (ValueError, FileNotFoundError, RuntimeError) as err:
            raise SystemExit(f'ошибка: {err}')
        broken += not r['цел']
        if r['цел']:
            print(f"цел {f} · импортируют: {', '.join(r['импортируют']) or '—'}")
        else:
            print(f"НЕ ЦЕЛ {r['где']}: {r['что']}\n    {r['кусок'][:160]}")
    raise SystemExit(1 if broken else 0)

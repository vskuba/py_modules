"""JS страницы как код: парсится ли он так, как его читает браузер (ESM), и где ломается.

Ближайший сосед — `file_check`, но он про байты (пустышка, огрызок, голова), а этот
модуль — про рантайм браузера. Смысл отдельного инструмента в том, что `node --check`
по `.js` молчит там, где браузер уже умер: `.js` читается CommonJS-парсером и до
доехавшей из шаблона многострочной строки не доходит — страница гибнет с
«Invalid or unexpected token», а стек пуст, потому что модуль не исполнился вовсе.
Здесь код парсится ровно так, как его парсит браузер, и наружу выходят адрес
поломки (с куском исходника) и граф импортов — кто кого подключает.

Инлайн-скрипт шаблона — тот же код, что и внешний `.js`, поэтому и он парсится
здесь: блоки `<script>` вынимаются из HTML с их же номерами строк и каждый
парсится своим парсером — модуль как модуль, классический скрипт как скрипт.
"""
import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Потолок одного парсинга, секунды: зависший node не должен держать вызывающего.
JS_CHECK_TIMEOUT = 10.0

# Блок `<script>`: атрибуты отдельно, тело отдельно. Внешний (с `src=`) — не
# сюда, его проверяй самим файлом; блоки с чужим `type=` (json, importmap) —
# не JS, браузер их не парсит и мы не делаем вид.
JS_CHECK_BLOCK = re.compile(r'<script([^>]*)>(.*?)</script>', re.I | re.S)
JS_CHECK_SRC = re.compile(r'\bsrc\s*=', re.I)
JS_CHECK_TYPE = re.compile(r'\btype\s*=\s*(["\'])\s*(.*?)\s*\1', re.I)
# Импорт: с «from» и без (побочный импорт — тоже связь графа). Кавышки внешние,
# внутренние не режем: в шаблонах путь живёт и внутри `{{ static_url('…') }}`.
JS_CHECK_IMPORT = re.compile(
    r'import\s+(?:[\s\S]*?\sfrom\s*)?(["\'])((?:(?!\1)[\s\S])+?)\1')

# stderr node: `<файл>:<номер>` — сама строка — каретка — `SyntaxError: <что>`.
JS_CHECK_AT = re.compile(r'^(\S+):(\d+)$', re.M)
JS_CHECK_ERR = re.compile(r'^(SyntaxError|TypeError|ReferenceError): (.*)$', re.M)


def js_check(path) -> dict:
    """Распарсится ли JS так, как его читает браузер; где поломка и кто кого импортирует.

    `.js`/`.mjs` парсятся целиком; из `.html`/`.htm` вынимаются все телесные
    блоки `<script>` (без `src`) и парсятся парсером своего рода: модуль
    `type=module` — как ESM, классический — как скрипт (импорт в классическом
    блоке браузер роняет — роняем и мы). В граф импортов идут только модули:
    классический блок ничего статически не импортирует.

    Args:
        path: путь к `.js`/`.mjs` или к странице `.html`/`.htm`.

    Returns:
        {'intact': bool, 'what': 'SyntaxError: …' или '', 'where': 'файл:строка',
         'chunk': строка исходника в месте поломки, 'imported_by': [адреса],
         'blocks': {'module': n, 'classic': n}}: у целого файла `what`/`where`/
         `chunk` пустые; `imported_by` — статический граф (куда смотрят импорты
         этого файла), пуст у страницы без модуля.

    ⚠ node — системная зависимость (как ffmpeg в `adb_rec`): нет бинаря —
    отдельная ошибка `FileNotFoundError`, а не молчаливый пропуск.
    """
    path = Path(path)
    code = path.read_text(encoding='utf-8', errors='replace')
    counted = {'module': 0, 'classic': 0}
    if path.suffix.lower() in ('.html', '.htm'):
        blocks = []
        for m in JS_CHECK_BLOCK.finditer(code):
            kind = _block_kind(m.group(1))
            if kind is None or not m.group(2).strip():
                continue
            counted['module' if kind else 'classic'] += 1
            blocks.append((1 + code.count('\n', 0, m.start(2)), m.group(2), kind))
    elif path.suffix.lower() in ('.js', '.mjs'):
        blocks = [(1, code, True)]
        counted = {'module': 1, 'classic': 0}
    else:
        raise ValueError(f'не код: {path.name} (ожидается .js/.mjs или .html)')

    edges, found = [], []
    for line0, body, is_module in blocks:
        if is_module:
            edges += [m.group(2) for m in JS_CHECK_IMPORT.finditer(body)]
        bad = _check(body, line0, str(path), is_module)
        if bad:
            found.append(bad)
    return {'intact': not found,
            'what': found[0]['what'] if found else '',
            'where': found[0]['where'] if found else '',
            'chunk': found[0]['chunk'] if found else '',
            'imported_by': sorted(set(edges)), 'blocks': counted}


# ── детали реализации ──

def _block_kind(attrs: str):
    """Род inline-блока: True модуль, False классический, None не JS вовсе."""
    if JS_CHECK_SRC.search(attrs):
        return None
    m = JS_CHECK_TYPE.search(attrs)
    kind = m.group(2).lower() if m else ''
    if kind == 'module':
        return True
    if kind == '' or 'javascript' in kind:
        return False
    return None  # application/json, importmap и прочее — не исполняемый JS


def _check(body: str, line0: int, where: str, module: bool = True) -> dict:
    """Один блок кода через node: {} когда цел, иначе находка.

    Род блока выбирает парсер, как в браузере. Модуль — `node --check` по `.mjs`
    (ESM-парсер). Классический скрипт — компиляция `vm.Script`: у `--check` по
    `.js` в node 24 цель модуля определяется сам и `import` в скрипте проходит
    молча, а браузер на нём падает — `vm.Script` же повторяет браузер вплоть до
    «Cannot use import statement outside a module» и номера строки.

    Тело пишется во временный файл с отступом строк до `line0` — чтобы номер в
    stderr node был родным для шаблона, без пересчёта на месте: кусок берётся из
    исходного файла по этому же номеру.
    """
    node = shutil.which('node')
    if not node:
        raise FileNotFoundError('node не найден в PATH — проверять целость кода '
                                'нечем (зависимость системная, как ffmpeg в adb_rec)')
    with tempfile.NamedTemporaryFile('w', suffix='.mjs' if module else '.js',
                                     delete=False, encoding='utf-8') as fh:
        fh.write('\n' * (line0 - 1) + body)
        tmp = fh.name
    argv = ([node, '--check', tmp] if module else
            [node, '-e', 'new (require("vm").Script)'
             '(require("fs").readFileSync(process.argv[1],"utf8"),'
             '{filename: process.argv[1]})', tmp])
    try:
        done = subprocess.run(argv, capture_output=True,
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
    return {'what': errm.group(0) if errm else err.strip()[:200],
            'where': (f'{where}:{number}' if at else where),
            'chunk': lines[i].strip() if 0 <= i < len(lines) else ''}


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
        broken += not r['intact']
        if r['intact']:
            kinds = r['blocks']
            print(f"цел {f} · блоков {kinds['module']} модуль + {kinds['classic']} скрипт"
                  f" · импортируют: {', '.join(r['imported_by']) or '—'}")
        else:
            print(f"НЕ ЦЕЛ {r['where']}: {r['what']}\n    {r['chunk'][:160]}")
    raise SystemExit(1 if broken else 0)

"""
Классы, которые JS лепит, против CSS, который о них знает: дыра видна числом.

Страницы панелей живут разметкой из JS (template literal'ы, `classList.toggle`)
и стилями шаблона; расходятся они молча — «js-класс без css» не роняет ничего,
просто выглядит не так и ловится глазами позже. Функция раскладывает пару
шаблон ↔ скрипты и отвечает числом с каждой стороны: что лепит JS и не описано
в CSS; что CSS знает и никому не нужно; какие id из JS не встали в разметку.

Грабли сверки: разметка в template literal'ах пестрит `${...}` — выражения
вырезаются целиком и условные классы из них не извлекаются (лучше недобор,
чем `${x ? 'done' : ''}` считалось классом); пары подаются как поданы — класс
из соседнего общего css-файла, не переданного в аргументы, честно встанет в
`js_unstyled`.
"""
import argparse
import re
from pathlib import Path

_STYLE = re.compile(r'<style[^>]*>(.*?)</style>', re.S | re.I)
_SCRIPT = re.compile(r'<script[^>]*>(.*?)</script>', re.S | re.I)
_CLASS_SEL = re.compile(r'\.([A-Za-z][\w-]*)')
_ATTR_CLASS = re.compile(r'class="([^"]*)"')
_ATTR_ID = re.compile(r'id="([\w-]+)"')
_JS_LIST = re.compile(r"classList\.\w+\(\s*(\[[^\]]*\]|['\"][^'\"]+['\"])")
_JS_ON = re.compile(r"className\s*=\s*['\"]([^'\"]+)['\"]")
_JS_ID = re.compile(r"getElementById\(['\"]([\w-]+)|querySelector(?:All)?\("
                    r"['\"]#([\w-]+)")
_SRC = re.compile(r'<script[^>]*\ssrc="([^"]+)"', re.I)
_IMPORT = re.compile(r"""\bimport\b[^;'"]*['"]([^'"]+)['"]""")
_GIVES = re.compile(r'\bwindow\.([A-Za-z_]\w*)\s*=')
# Верхнеуровневая функция — ровно с нулевой колонки. Вложенная в IIFE наружу
# не торчит, и считать её глобалью значит обещать то, чего нет.
_DECLARES = re.compile(r'^(?:async\s+)?function\s+([A-Za-z_]\w*)', re.M)
# Своё имя страницы — с любым отступом и в любой форме. Строже нельзя: `load`
# объявляют внутри блока почти на каждой второй странице, и строгий якорь
# выдавал чужой `function load` за недостающую глобаль.
_OWN = re.compile(r'^\s*(?:(?:async\s+)?function\s+|(?:const|let|var)\s+)'
                  r'([A-Za-z_]\w*)', re.M)
_MODULE = re.compile(r'^\s*(?:import\b[^;]*?[\'"]|export\b)', re.M)
_EXTENDS = re.compile(r'{%-?\s*extends\s+[\'"]([^\'"]+)[\'"]')
_CALLS = re.compile(r'(?<![.\w$])([A-Za-z_]\w*)\s*\(')


def html_scan_pairs(html_paths, js_paths=()) -> dict:
    """Пара шаблон ↔ скрипты: чем разъехались классы и id.

    Args:
        html_paths: шаблоны (с инлайн-`<style>` и разметкой).
        js_paths: скрипты; пусто — только инлайн-скрипты шаблонов.

    Returns:
        {'js_unstyled': классы из JS без CSS, 'css_unused': селекторы CSS,
         о которых не знает ни JS, ни разметка, 'ids_missing': id из JS,
         которых нет в разметке}.
    """
    html_text = '\n'.join(Path(p).read_text(encoding='utf-8')
                          for p in map(str, html_paths))
    js_text = '\n'.join(Path(p).read_text(encoding='utf-8')
                       for p in map(str, js_paths)) + '\n' + \
        '\n'.join(_SCRIPT.findall(html_text))

    css = set()
    for block in _STYLE.findall(html_text):
        for sel in (chunk.rsplit('}', 1)[-1] for chunk in block.split('{')):
            css.update(_CLASS_SEL.findall(sel))
    markup_cls = set()
    for val in _ATTR_CLASS.findall(html_text):
        markup_cls |= _tokens(val)
    markup_id = set(_ATTR_ID.findall(html_text))

    js_cls = set()
    for blob in (_JS_LIST.findall(js_text) + _JS_ON.findall(js_text)
                 + _ATTR_CLASS.findall(js_text)):
        js_cls |= _tokens(blob)
    js_id = {g for tup in _JS_ID.findall(js_text) for g in tup if g}

    return {'js_unstyled': sorted(js_cls - css - markup_cls),
            'css_unused': sorted(css - js_cls - markup_cls),
            'ids_missing': sorted(js_id - markup_id)}


def html_scan_globals(html_paths, roots, prefix: str = '/static/',
                      templates=()) -> dict:
    """Глобали, которые страница зовёт, но не подключает: кто и чем лечится.

    Args:
        html_paths: шаблоны страниц.
        roots: каталоги раздачи статики, в порядке поиска (свой, потом общий) —
            по ним разрешается `src="/static/js/x.js"`.
        prefix: под каким путём смонтирована раздача.
        templates: каталоги шаблонов, в том же порядке — по ним разрешается
            `{% extends %}`. ⚠⚠ Без них проверка врёт на каждой странице,
            которая живёт наследованием: скрипт подключает **родитель**
            (`base.html` грузит `tz.js` всем сразу), и страница, законно на это
            опирающаяся, объявлялась бы забывшей подключение.

    Returns:
        {'missing': [{'name': звавшаяся глобаль, 'gives': файл, который её
         объявляет, 'page': шаблон}], 'unresolved': ссылки `src`, которых нет
        ни в одном каталоге}.

    Зачем отдельно от `html_scan_pairs`: тот сверяет имена **внутри** пары, а
    здесь вопрос другой — хватает ли странице того, что она грузит.

    ⚠⚠ Ловит поломку, которой **не видно ничем другим**. Страница общего
    каркаса звала `loadTz()`, а объявлял её `tz.js`, подключённый в `base.html`
    только одного проекта. У соседа страница не работала вовсе: вёрстка
    рисовалась, обе колонки навсегда в «Загрузка…», в журнале чисто, в консоли
    один `ReferenceError`. Ни кадром, ни сюитой, ни `js_check` это не ловится:
    каждый файл сам по себе цел, не сходится только их набор на странице.

    ⚠ Отвечает не «неизвестное имя», а **именем файла, который лечит**: список
    поставщиков собирается по `window.X =` и верхнеуровневым `function X` во
    всех файлах каталогов. Имя, которого не даёт никто, не считается находкой —
    это браузерный API или чужая библиотека, и шуметь на них нельзя.

    ⚠ Зовущая сторона — сам шаблон, его inline-скрипты и все подключённые
    файлы вместе с их `import`-графом: глобаль зовут обычно из модуля, а
    подключают строкой в шаблоне, и разъезжаются именно эти два места.
    """
    roots = [Path(str(r)) for r in roots]
    tpl_roots = [Path(str(t)) for t in templates]
    gives = _html_scan_suppliers(roots)

    out, unresolved = [], []
    for page in map(str, html_paths):
        text = _html_scan_inherit(Path(page), tpl_roots)

        loaded, code = set(), [text]
        for src in _SRC.findall(text):
            found = _html_scan_resolve(src, roots, prefix)
            if found is None:
                # Наша ссылка, но файла нет — находка. Чужая (CDN, внешний
                # адрес) — молчим: за чужой диск мы не отвечаем.
                if src.startswith(prefix) or '{{' in src:
                    unresolved.append(src)
                continue
            _html_scan_follow(found, roots, prefix, loaded, code)

        body = '\n'.join(code)
        # Объявленное на самой странице глобалью не считается: вопрос в том,
        # чего ей не хватает, а не в том, что она определяет у себя.
        own = set(_OWN.findall(body)) | set(_GIVES.findall(body))
        for name in set(_CALLS.findall(body)) - own:
            supplier = gives.get(name)
            if supplier and supplier not in loaded:
                out.append({'name': name, 'gives': supplier, 'page': page})

    return {'missing': sorted(out, key=lambda one: (one['page'], one['name'])),
            'unresolved': sorted(set(unresolved))}


def _tokens(blob: str) -> set:
    """Имена классов из куска разметки: `${...}`-выражения вырезаются целиком
    (условные классы из них не извлекаем — это честная потеря, не мусор);
    годятся только осмысленные имена, не обрывки кода."""
    clean = re.sub(r'\$\{[^{}]*\}', ' ', blob)
    return {t for t in re.split(r"[\s'\"]+", clean)
            if re.fullmatch(r'[A-Za-z][\w-]*', t)}


def _html_scan_suppliers(roots) -> dict:
    """Имя глобали → файл, который её объявляет.

    Глобалью считается `window.X =` у любого файла и верхнеуровневая
    `function X` — **только у обычного скрипта**.

    ⚠⚠ У ES-модуля верхнеуровневая функция наружу не торчит вовсе: у него своя
    область видимости. Считать её глобалью — прямой путь к ложной тревоге на
    общих именах: `journal.js` объявляет `function load`, и страница со своим
    `load` объявлялась «забывшей подключить journal.js».

    ⚠ Каталоги просматриваются в обратном порядке, чтобы первый в списке
    перетирал последующие: раздача ищет файл сперва у проекта, и свой файл с
    тем же именем обязан перекрывать общий здесь так же, как там.
    """
    gives = {}
    for root in reversed(list(roots)):
        for path in sorted(root.rglob('*.js')):
            try:
                text = path.read_text(encoding='utf-8')
            except OSError:
                continue

            names = set(_GIVES.findall(text))
            if not _MODULE.search(text):
                names |= set(_DECLARES.findall(text))
            for name in names:
                gives[name] = path.name
    return gives


def _html_scan_inherit(page, tpl_roots, seen=None) -> str:
    """Текст страницы вместе с её родителями по `{% extends %}`.

    Родитель приклеивается целиком, а не разбирается по блокам: вопрос здесь —
    что на странице **подключено и позвано**, а не какой блок кого перекрыл.

    ⚠ Каталоги шаблонов не заданы — возвращаем страницу как есть. Молча, без
    отказа: проверка одиночного файла тоже имеет смысл, просто она уже.
    """
    text = page.read_text(encoding='utf-8')
    if not tpl_roots:
        return text

    seen = seen or {page.resolve()}
    for ref in _EXTENDS.findall(text):
        for root in tpl_roots:
            parent = root / ref
            if not parent.is_file() or parent.resolve() in seen:
                continue
            seen.add(parent.resolve())
            return text + '\n' + _html_scan_inherit(parent, tpl_roots, seen)
    return text


def _html_scan_resolve(src: str, roots, prefix: str):
    """Ссылка `src` → файл на диске; `None` — не нашёлся или ведёт наружу.

    ⚠⚠ Понимает и шаблонный помощник: `{{ static_url('js/x.js') }}`,
    `{{ url_for('static', filename='js/x.js') }}`. Литеральным путём статику
    подключают далеко не везде, и разбор, знающий только `/static/…`, объявлял
    бы «не подключено» на странице, где подключено помощником — то есть ровно
    там, где всё в порядке.
    """
    tail = ''
    if '{{' in src:
        # Из выражения берём первый довод, похожий на файл: у `static_url` он
        # единственный, у `url_for('static', filename=…)` первым идёт имя
        # раздачи, и брать «первый кавычный» было бы неверно.
        for token in re.findall(r"['\"]([^'\"]+)['\"]", src):
            if re.search(r'\.\w+$', token):
                tail = token.lstrip('/')
                break
    elif src.startswith(prefix):
        tail = src[len(prefix):]

    tail = tail.split('?', 1)[0]
    if not tail:
        return None

    for root in roots:
        found = root / tail
        if found.is_file():
            return found
    return None


def _html_scan_follow(path, roots, prefix: str, loaded: set, code: list) -> None:
    """Подключить файл и всё, что он импортирует, в набор страницы.

    ⚠ Обход с защитой от повторов: модули ссылаются друг на друга кругами, и
    без `loaded` первый же такой круг уводил бы в бесконечность.
    """
    if path.name in loaded:
        return
    loaded.add(path.name)
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return

    code.append(text)
    for ref in _IMPORT.findall(text):
        found = _html_scan_resolve(ref, roots, prefix)
        if found is not None:
            _html_scan_follow(found, roots, prefix, loaded, code)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Классы/ид из JS против шаблона: чем разъехались.')
    ap.add_argument('html', nargs='+', help='шаблон(ы)')
    ap.add_argument('--js', default='', help='скрипты через запятую')
    ap.add_argument('--globals', default='',
                    help='каталоги раздачи через запятую (свой, потом общий): '
                         'проверить, что страница подключает звавшиеся глобали')
    # ⚠ `%%`, а не `%`: argparse прогоняет help через %-форматирование, и живой
    # процент в тексте роняет даже `--help`, а не только эту ветку.
    ap.add_argument('--templates', default='',
                    help='каталоги шаблонов через запятую — разрешать '
                         '{%% extends %%}; без них страницы с наследованием врут')
    ns = ap.parse_args()
    try:
        if ns.globals:
            res = html_scan_globals(
                ns.html, ns.globals.split(','),
                templates=ns.templates.split(',') if ns.templates else ())
            for one in res['missing']:
                print(f"🔴 {one['page']}: зовёт {one['name']}(), "
                      f"но не подключает {one['gives']}")
            for src in res['unresolved']:
                print(f'⚠ {src}: файла нет ни в одном каталоге')
            raise SystemExit(len(res['missing']) + len(res['unresolved']))

        res = html_scan_pairs(ns.html, ns.js.split(',') if ns.js else ())
        for key in ('js_unstyled', 'css_unused', 'ids_missing'):
            print(f'{key} ({len(res[key])}): {", ".join(res[key]) or "—"}')
    except (RuntimeError, ValueError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')

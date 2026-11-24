"""
Приметы overengineering в PHP — те же, что у `code_overengineering`, по разбору nikic/php-parser.

    PYTHONPATH=py_modules python -m code_.code_overengineering src processors   # .php берутся сами

Факты (классы, методы, параметры, тела-обёртки, вызовы своих методов) достаёт
`code_overengineering_php_dump.php` одним запуском хостового `php` на все файлы;
правила судит Python — одним словарём с питоньей половиной, чтобы приметы не
разъехались по двум языкам.

| kind | в PHP |
|------|-------|
| `passthrough` | метод, чьё тело — один вызов со своими параметрами по порядку |
| `fixed_param` | у **private**-метода параметр с умолчанием, а вызовы `$this->`/`self::`/`static::` его не меняют или всегда передают один литерал |
| `single_impl` | interface / abstract class с ≤1 реализацией под корнем |

⚠⚠ Не находка (шум первого прогона по cashier-payment-gateway, 226 → см. `docs/code_overengineering.md`):
методы протоколов SPL (`current`, `offsetGet`, … — делегирование требует интерфейс),
фабричные методы `createX(): new X(...)` — точка подмены в наследнике, и входы каркаса
(PHP-атрибуты, `@Rest\\Route`/`@Route` в докблоке) — их зовут не по имени.

⚠⚠ `class_as_function` для PHP **выключен**: в Symfony любой сервис — конструктор с
зависимостями и один метод (обработчик, команда, резолвер). Примета дала бы сотни
находок, и все про устройство фреймворка, а не про переусложнение.

⚠⚠ Парсер берётся из **vendor проекта** (`nikic/php-parser` ставит psalm/phpunit), а не
из composer-автозагрузчика: проектный autoload проверяет платформу и падает на хостовом
PHP другой версии. Нет парсера или `php` — PHP молча пропускается (`code_overengineering`
говорит об этом в отчёте).

⚠ Интерфейс с одной реализацией в Symfony часто держат ради подмены в тестах
(`createMock(XInterface::class)`) — это кандидат, а не приговор; оставляешь — причину в
докблок интерфейса.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

CODE_OVERENGINEERING_PHP_DUMP = Path(__file__).with_name('code_overengineering_php_dump.php')
CODE_OVERENGINEERING_PHP_PARSER = Path('nikic') / 'php-parser' / 'lib' / 'PhpParser' / 'ParserFactory.php'
CODE_OVERENGINEERING_PHP_TIMEOUT = 300
# Методы протоколов SPL/JSON — делегирование, которого требует интерфейс (как dunder в питоне).
CODE_OVERENGINEERING_PHP_PROTOCOL = ('current', 'key', 'next', 'rewind', 'valid', 'count', 'offsetGet',
                                     'offsetSet', 'offsetExists', 'offsetUnset', 'getIterator',
                                     'jsonSerialize', 'getChildren', 'hasChildren', 'accept')


def code_overengineering_php(files, root, texts=None) -> tuple:
    """Приметы по `.php`-файлам: (находки, причина пропуска или '').

    Args:
        files: пути `.php`.
        root: корень, где ищутся реализации интерфейсов и абстрактных классов.
        texts: кэш текстов корня `{путь: текст}` — делится с вызывающим.

    Returns:
        tuple: (list[dict] находок {'kind','file','line','name','hint'}, str — почему PHP
            не разобран: нет `php`, нет парсера в vendor; '' — разобран).
    """
    if not files:
        return [], ''

    php = shutil.which('php')
    vendor = _vendor(files, root)

    if not php or not vendor:
        return [], 'PHP пропущен: ' + ('нет `php` в PATH' if not php else 'нет nikic/php-parser в vendor')

    run = subprocess.run([php, '-d', 'memory_limit=2G', str(CODE_OVERENGINEERING_PHP_DUMP), str(vendor)],
                         input='\n'.join(str(f) for f in files), capture_output=True, text=True,
                         timeout=CODE_OVERENGINEERING_PHP_TIMEOUT, check=False)

    try:
        dump = json.loads(run.stdout or '{}')
    except json.JSONDecodeError:
        return [], f'PHP пропущен: разбор упал ({(run.stderr or run.stdout)[:200]})'

    texts = {} if texts is None else texts
    out = []

    for path, classes in dump.items():
        for cls in classes or []:
            out.extend(dict(one, file=path) for one in _passthrough(cls))
            out.extend(dict(one, file=path) for one in _fixed_param(cls))
            out.extend(dict(one, file=path) for one in _single_impl(cls, Path(root), texts))

    return out, ''


def _php_files(root) -> list:
    """`.php` под корнем без vendor и служебных — где искать реализации.

    ⚠ Свой обход, а не `code_overengineering_files`: тот модуль импортирует этот, и
    обратный импорт замкнул бы круг.
    """
    skip = ('vendor', 'node_modules', '.git', 'var', 'cache')

    return [p for p in Path(root).rglob('*.php') if not any(part in skip for part in p.parts)]


def _vendor(files, root) -> Path | None:
    """vendor с nikic/php-parser: `CODE_OVERENGINEERING_PHP_VENDOR`, иначе вверх от корня и от первого файла."""
    hint = os.environ.get('CODE_OVERENGINEERING_PHP_VENDOR')

    if hint and (Path(hint) / CODE_OVERENGINEERING_PHP_PARSER).is_file():
        return Path(hint)

    starts = [Path(root).resolve(), Path(files[0]).resolve().parent]

    for start in starts:
        for one in (start, *start.parents):
            if (one / 'vendor' / CODE_OVERENGINEERING_PHP_PARSER).is_file():
                return one / 'vendor'

    return None


def _passthrough(cls) -> list:
    """Методы-обёртки: тело — вызов со своими параметрами по порядку."""
    private = {m['name'] for m in cls['methods'] if m['private']}
    out = []

    for m in cls['methods']:
        fwd = m.get('forward')

        if (not fwd or m['name'].startswith('__') or m['abstract'] or m.get('routed')
                or m['name'] in CODE_OVERENGINEERING_PHP_PROTOCOL):
            continue

        params = [p['name'] for p in m['params'] if not p['variadic']]
        target = fwd['target']

        if target.startswith('$this->') and target[len('$this->'):] in private:
            continue           # публичная дверь к своей private-реализации — §4.3

        if target.startswith('new '):
            continue           # фабричный метод: точка подмены в наследнике (шаблон процессоров), не второе имя

        if params and fwd['args'] == params:
            out.append({'kind': 'passthrough', 'line': m['line'], 'name': f"{cls['name']}::{m['name']}",
                        'hint': f'только передаёт параметры в `{target}` — второе имя одного действия'})

    return out


def _fixed_param(cls) -> list:
    """Параметры private-методов, которые вызовы класса не меняют или передают одним литералом."""
    calls = {}

    for m in cls['methods']:
        for call in m['calls']:
            calls.setdefault(call['name'], []).append(call)

    out = []

    for m in cls['methods']:
        mine = calls.get(m['name'])

        if not m['private'] or not mine or any(c['opaque'] for c in mine):
            continue

        for index, p in enumerate(m['params']):
            if p['default'] is None or p['default'].lower() == 'null' or p['variadic']:
                continue       # `null` — точка подмены, как `None` в питоне

            values = []

            for c in mine:
                given = (c['named'] or {}).get(p['name']) or (c['pos'][index] if index < len(c['pos']) else None)
                if given:
                    values.append(given['value'])

            name = f"{cls['name']}::{m['name']}(${p['name']})"

            if not values:
                out.append({'kind': 'fixed_param', 'line': m['line'], 'name': name,
                            'hint': f"ни один из {len(mine)} вызовов не меняет умолчание "
                                    f"`{p['default']}` — опция без пользователя"})
            elif len(values) == len(mine) > 1 and len(set(values)) == 1 and values[0] is not None:
                out.append({'kind': 'fixed_param', 'line': m['line'], 'name': name,
                            'hint': f'все {len(mine)} вызова передают `{values[0]}` — '
                                    'параметр не различает ничего'})

    return out


def _single_impl(cls, root, texts) -> list:
    """Интерфейсы и абстрактные классы с ≤1 реализацией под корнем (поиск текстом)."""
    if cls['kind'] not in ('interface', 'abstract'):
        return []

    heirs = _heirs(cls['name'], root, texts)

    # Единственный наследник сам абстрактный со своими реализациями (интерфейс →
    # абстрактный класс → провайдеры) — слой не пустой, считаем транзитивно.
    if len(heirs) == 1 and len(_heirs(heirs[0][1], root, texts)) > 1:
        return []

    if len(heirs) > 1:
        return []

    who = f'единственная — `{heirs[0][1]}` ({heirs[0][0]})' if heirs else 'ни одной'
    what = 'интерфейс' if cls['kind'] == 'interface' else 'абстрактный класс'

    return [{'kind': 'single_impl', 'line': cls['line'], 'name': cls['name'],
             'hint': f'{what}, реализаций {who}'}]


def _heirs(name, root, texts) -> list:
    """Пары (файл, класс) под корнем, у которых `name` стоит в extends/implements.

    ⚠⚠ Считаются пары, а не имена: у каждого провайдера свой `ApiManager`, и по именам
    95 реализаций `AbstractApiManager` схлопывались в одну.
    """
    if not texts:
        for spot in _php_files(root):
            try:
                texts[spot] = spot.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue

    pattern = re.compile(r'\b(?:class|interface|enum)\s+(\w+)[^{;]*?\b(?:extends|implements)\b[^{;]*?\b'
                         + re.escape(name) + r'\b[^{;]*\{', re.S)
    out = set()

    for spot, text in texts.items():
        if name in text:
            out.update((str(spot), m.group(1)) for m in pattern.finditer(text) if m.group(1) != name)

    return sorted(out)

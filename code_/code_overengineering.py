"""
Приметы overengineering в готовом коде: обвязка, пережившая свою причину.

    PYTHONPATH=py_modules python -m code_.code_overengineering src            # дерево целиком
    PYTHONPATH=py_modules python -m code_.code_overengineering a.py b.py      # файлы партии

Новый код редко пишут «на вырост» — переусложнённым его делают правки. Было три
реализации, две сняли, а абстракция осталась; было два режима, второй больше никто
не включает, а параметр и ветка под него живут; функцию переименовали, а старое имя
оставили обёрткой «на всякий случай». Каждый такой след по отдельности безвреден и
выглядит частью устройства — читатель строит на нём рассуждение («тут, видимо,
несколько реализаций») и тратит время на механику, которой давно нет.

Поэтому инструмент смотрит **файл целиком, а не дифф**: застава ревизии
(`claude_gate_code_review`) зовёт его по файлам партии, и агент, зашедший в старый
файл ради одной строки, видит остатки прошлых правок вокруг неё.

## Приметы — только те, что решаются без контекста

| kind | что найдено | почему это overengineering |
|------|-------------|----------------------------|
| `passthrough` | функция только передаёт свои параметры другой | лишнее имя: второй вход в одно и то же |
| `fixed_param` | у приватной функции параметр с умолчанием, а вызовы его не меняют или всегда передают одно значение | вариативность, которой больше нет |
| `single_impl` | абстрактный класс (ABC / Protocol / одни заглушки) с ≤1 наследником | абстракция над одним случаем |
| `class_as_function` | класс из `__init__` и одного метода | состояние ради одного вызова |

⚠⚠ `fixed_param` судит **только приватные** функции. У приватной вызывающие живут в
её же файле — вывод точный; у публичной они могут быть в любом проекте, куда
подключён слой, и «здесь никто не передаёт» не значит ничего (та же причина, что у
`code_dead --private`).

⚠ `single_impl` ищет наследников текстом по `root` (`class X(...Base...)`), а не графом:
регистрация через реестр, `type()` или конфиг его обманет. Поэтому это кандидат.

PHP разбирается соседом `code_overengineering_php` (nikic/php-parser из vendor проекта,
те же приметы, кроме `class_as_function`). Не нашлось `php` или парсера — в отчёте строка
`skipped`, а не тихое «чисто». JS и прочее — `code_rules.md` §4.5, смотрит тот, кто правил.

## Чего здесь нет намеренно

«Слишком много слоёв», «фабрика ради фабрики», «конфиг, который никто не меняет» —
судейские вопросы: машина без контекста даст на них больше ложных находок, чем
верных, а ложная находка «упрости» стоит дороже молчания — её выполняют.
"""
import argparse
import ast
import re
from pathlib import Path

from code_.code_overengineering_php import code_overengineering_php

CODE_OVERENGINEERING_SKIP = ('node_modules', '.git', 'dist', 'build', 'vendor', '.venv', 'venv',
                             '__pycache__', '.mypy_cache')
CODE_OVERENGINEERING_ABSTRACT_BASES = ('ABC', 'Protocol', 'ABCMeta')


def code_overengineering(paths, root=None) -> list[dict]:
    """Найти в файлах обвязку, пережившую свою причину: обёртки, мёртвые опции, абстракции над одним случаем.

    Args:
        paths: пути `.py`/`.php` или каталоги; каталог обходится целиком.
        root: где искать наследников для `single_impl`; пусто — общий корень `paths`.

    Returns:
        list[dict]: {'kind', 'file', 'line', 'name', 'hint'} по файлам и строкам.
            Пусто — приметы не нашлись (а не «переусложнения нет»: судейские вопросы
            инструмент не решает, см. докстринг модуля).

    ⚠ Нечитаемый или неразбираемый файл молча пропускается — находки с остальных
    важнее, чем падение на одном (застава зовёт это на каждой партии правок).
    """
    files = code_overengineering_files(paths)
    php_files = code_overengineering_files(paths, ('.php',))
    base = Path(root) if root else _common_root(files + php_files)
    texts = {}              # тексты корня для поиска наследников — читаются один раз, лениво
    out = []

    for spot in files:
        try:
            tree = ast.parse(spot.read_text(encoding='utf-8'))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue

        out.extend(dict(one, file=str(spot)) for one in _passthrough(tree))
        out.extend(dict(one, file=str(spot)) for one in _fixed_param(tree))
        out.extend(dict(one, file=str(spot)) for one in _class_as_function(tree))
        out.extend(dict(one, file=str(spot)) for one in _single_impl(tree, base, texts))

    php_found, php_skip = code_overengineering_php(php_files, base)
    out.extend(php_found)

    if php_skip:
        out.append({'kind': 'skipped', 'file': str(base), 'line': 0, 'name': f'{len(php_files)} .php',
                    'hint': php_skip})

    return sorted(out, key=lambda one: (one['file'], one['line'], one['kind']))


def code_overengineering_files(paths, suffixes=('.py',)) -> list:
    """Файлы с нужными расширениями из списка путей; каталоги — обходом без служебных (vendor и т.п.)."""
    out = []

    for one in paths or []:
        spot = Path(one)

        if spot.is_dir():
            out.extend(p for p in sorted(spot.rglob('*'))
                       if p.suffix in suffixes and p.is_file()
                       and not any(part in CODE_OVERENGINEERING_SKIP for part in p.parts))
        elif spot.suffix in suffixes and spot.is_file():
            out.append(spot)

    return out


def code_overengineering_format(found) -> str:
    """Отчёт словами по файлам. Пусто — `Примет не нашлось.`"""
    if not found:
        return 'Примет не нашлось.'

    out = [f'Кандидатов: {len(found)}.']
    file_now = ''

    for one in found:
        if one['file'] != file_now:
            file_now = one['file']
            out.append(f'\n── {file_now}')

        out.append(f"   {one['line']:>5}  {one['kind']:17} {one['name']} — {one['hint']}")

    out.append('\n⚠ Это кандидаты: обёртка бывает публичным именем для совместимости, '
               'абстракция — границей чужого формата. Оставляешь — причину в докстринг.')

    return '\n'.join(out)


def main() -> int:
    """CLI: код возврата — число находок (0 — чисто)."""
    ap = argparse.ArgumentParser(description='Приметы overengineering в готовом коде.')
    ap.add_argument('paths', nargs='*', default=['.'], help='файлы или каталоги')
    ap.add_argument('--root', default=None, help='где искать наследников абстракций')
    ns = ap.parse_args()

    found = code_overengineering(ns.paths, ns.root)
    print(code_overengineering_format(found))

    return min(len(found), 100)




def _common_root(files) -> Path:
    """Общий каталог файлов — по нему ищутся наследники."""
    if not files:
        return Path('.')

    parents = [set(p.resolve().parents) for p in files]
    common = set.intersection(*parents)

    return max(common, key=lambda p: len(p.parts)) if common else Path('.')


def _passthrough(tree) -> list:
    """Функции, чьё тело — один `return f(...)` со своими же параметрами по порядку."""
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.decorator_list:
            continue

        if node.name.startswith('__') and node.name.endswith('__'):
            continue           # протокол (`__aexit__` → вложенный объект) — не второе имя

        body = _body(node)

        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue

        call = body[0].value
        call = call.value if isinstance(call, ast.Await) else call

        if not isinstance(call, ast.Call):
            continue

        if isinstance(call.func, ast.Name) and call.func.id.startswith('_'):
            continue           # публичная дверь к своей приватной реализации — §4.3, не лишний слой

        params = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                  if a.arg not in ('self', 'cls')]
        passed = [a.id for a in call.args if isinstance(a, ast.Name)]
        passed += [k.value.id for k in call.keywords if isinstance(k.value, ast.Name)]
        extra = len(call.args) + len(call.keywords) - len(passed)

        if params and passed == params and not extra:
            out.append({'kind': 'passthrough', 'line': node.lineno, 'name': node.name,
                        'hint': f'только передаёт параметры в `{ast.unparse(call.func)}` — '
                                'второе имя одного действия'})

    return out


def _fixed_param(tree) -> list:
    """Параметры приватных функций, которые вызовы в файле не меняют или передают одним значением."""
    calls = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name:
                calls.setdefault(name, []).append(node)

    out = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if not node.name.startswith('_') or node.name.startswith('__') or node.name not in calls:
            continue

        out.extend(_fixed_param_of(node, calls[node.name]))

    return out


def _fixed_param_of(func, calls) -> list:
    """Находки `fixed_param` у одной функции по всем её вызовам."""
    positional = [a.arg for a in func.args.posonlyargs + func.args.args]
    offset = 1 if positional[:1] in (['self'], ['cls']) else 0
    defaults = dict(zip(positional[len(positional) - len(func.args.defaults):], func.args.defaults))
    defaults.update({a.arg: d for a, d in zip(func.args.kwonlyargs, func.args.kw_defaults) if d is not None})
    out = []

    for param, default in defaults.items():
        if isinstance(default, ast.Constant) and default.value is None:
            continue           # `argv=None` и подобное — точка подмены для тестов, они в другом файле

        index = positional.index(param) - offset if param in positional else None
        values = []

        for call in calls:
            if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                values = None          # *args / **kwargs — не видно, что передано
                break

            given = next((k.value for k in call.keywords if k.arg == param), None)

            if given is None and index is not None and 0 <= index < len(call.args):
                given = call.args[index]

            if given is not None:
                values.append(_literal(given))

        if values is None:
            continue

        if not values:
            out.append({'kind': 'fixed_param', 'line': func.lineno, 'name': f'{func.name}({param})',
                        'hint': f'ни один из {len(calls)} вызовов не меняет умолчание '
                                f'`{ast.unparse(default)}` — опция без пользователя'})
        elif len(values) == len(calls) > 1 and len(set(values)) == 1 and values[0] is not None:
            out.append({'kind': 'fixed_param', 'line': func.lineno, 'name': f'{func.name}({param})',
                        'hint': f'все {len(calls)} вызова передают `{values[0]}` — '
                                'параметр не различает ничего'})

    return out


def _class_as_function(tree) -> list:
    """Классы, у которых кроме `__init__` ровно один метод и нет наследования."""
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.bases or node.decorator_list:
            continue

        methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        names = {m.name for m in methods}
        others = [m for m in methods if m.name != '__init__']

        if '__init__' in names and len(others) == 1 and not others[0].name.startswith('__'):
            out.append({'kind': 'class_as_function', 'line': node.lineno, 'name': node.name,
                        'hint': f'`__init__` + `{others[0].name}` — хватило бы функции'})

    return out


def _single_impl(tree, base, texts) -> list:
    """Абстрактные классы файла, у которых по корню нашлось не больше одного наследника."""
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_abstract(node):
            continue

        heirs = _heirs(node.name, base, texts)

        if len(heirs) <= 1:
            who = f'единственный — `{heirs[0]}`' if heirs else 'ни одного'
            out.append({'kind': 'single_impl', 'line': node.lineno, 'name': node.name,
                        'hint': f'абстракция, наследников {who}'})

    return out


def _is_abstract(node) -> bool:
    """ABC / Protocol по базам, либо все методы — заглушки (`...`, `pass`, `raise NotImplementedError`)."""
    bases = {ast.unparse(b).split('.')[-1].split('[')[0] for b in node.bases}
    bases |= {ast.unparse(k.value).split('.')[-1] for k in node.keywords if k.arg == 'metaclass'}

    if bases & set(CODE_OVERENGINEERING_ABSTRACT_BASES):
        return True

    methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]

    return bool(methods) and all(_is_stub(m) for m in methods)


def _is_stub(func) -> bool:
    """Тело метода — заглушка без поведения."""
    body = _body(func)

    if len(body) != 1:
        return False

    one = body[0]

    # ⚠ `pass` заглушкой не считается: класс из одних `pass` — это намеренный
    # null-object («тихий» логгер, пустой обработчик), а не абстракция.
    if isinstance(one, ast.Expr) and isinstance(one.value, ast.Constant) and one.value.value is Ellipsis:
        return True

    return isinstance(one, ast.Raise) and 'NotImplementedError' in ast.unparse(one)


def _heirs(name, base, texts) -> list:
    """Имена классов под `base`, у которых `name` стоит в базах (поиск текстом)."""
    pattern = re.compile(r'^\s*class\s+(\w+)\s*\(([^)]*\b' + re.escape(name) + r'\b[^)]*)\)', re.M)
    out = []

    if not texts:
        for spot in code_overengineering_files([base]):
            try:
                texts[spot] = spot.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue

    for text in texts.values():
        if name in text:
            out.extend(m.group(1) for m in pattern.finditer(text) if m.group(1) != name)

    return sorted(set(out))


def _literal(node):
    """Текст литерала (число, строка, кортеж констант) или None — значение не видно из вызова.

    ⚠ «Все вызовы передают одно» судится только для литералов: одинаковое имя
    переменной в трёх местах — три разных значения.
    """
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None

    return ast.unparse(node)


def _body(func) -> list:
    """Тело функции без докстринга."""
    body = list(func.body)

    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]

    return body


def _call_name(call) -> str:
    """Имя вызываемого: `f(...)` → f, `self.f(...)` / `x.f(...)` → f."""
    if isinstance(call.func, ast.Name):
        return call.func.id

    return call.func.attr if isinstance(call.func, ast.Attribute) else ''


if __name__ == '__main__':
    raise SystemExit(main())

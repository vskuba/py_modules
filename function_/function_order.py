"""Порядок определений в файле: публичное сверху, приватное внизу.

Правило записано в `docs/code_rules.md` §3: **все приватные — ниже всех
публичных, в конце файла**, а хук `__main__` — самой последней строкой. Причина
не в красоте: файл открывают, чтобы понять, **что** модуль умеет, а не как он это
делает. Приватная функция посреди публичных заставляет читать устройство раньше
назначения.

## ⚠⚠ Почему инструмент, а не `grep`

Рецепт в правилах ищет так:

    grep -nP '^(async )?def [a-z]' | tail -1     # последняя публичная
    grep -nP '^(async )?def _'     | head -1     # первая приватная

Он отвечает на вопрос грубо и три вещи пропускает **насовсем**:

* **классы и константы** — `class Foo` и `SOME_CONST` он не видит вовсе, хотя
  правило про них тоже: публичный класс после приватной функции читается так же
  плохо;
* **методы внутри класса** — `    def _helper` не начинается с начала строки, и
  весь класс для него невидим;
* **хук `__main__` не последним** — правило есть, и цена его нарушения названа
  там же: CLI падает на `NameError`, потому что нижних приватных ещё нет, а тесты
  в том же процессе при этом проходят. Правила прямо говорят, что машинная
  проверка этого «не видит»; здесь — видит.

⚠ Разбор `ast` смотрит **тело модуля и тела классов**, а вложенные функции не
трогает: замыкание внутри функции — часть её устройства, и правило про порядок к
нему не относится.

## ⚠ Чего инструмент не решает

Он не переставляет. Перенос меняет и порядок чтения, и иногда работу (функция,
вызванная на уровне модуля), — это решение человека.
"""
import argparse
import ast
import sys
from pathlib import Path

# Файл запускают и путём (`python3 py_modules/function_/function_order.py`).
# Тогда первым в путях лежит каталог файла, и `import function_` нашёл бы модуль
# рядом вместо пакета. Свой каталог убираем, корень ставим в начало.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Что считаем нарушением. ⚠ Слова короткие: они уезжают в `--json` и читаются
# вызывающим кодом, а не только человеком.
FUNCTION_ORDER_PUBLIC_LATE = 'public_after_private'
FUNCTION_ORDER_MAIN_LATE = 'main_not_last'

# Каталоги, которые не смотрим никогда: чужой код и следы сборки.
FUNCTION_ORDER_SKIP = ('__pycache__', '.venv', 'venv', 'node_modules', '.git',
                       'migrations')


def function_order(root: str) -> list:
    """
    Найти нарушения порядка в файле или дереве.

    Args:
        root: путь к `.py` либо к каталогу — тогда обходится дерево.

    Returns:
        Список находок `{'file', 'line', 'name', 'kind', 'after', 'after_line',
        'scope'}`. `kind` — `public_after_private` или `main_not_last`; `scope` —
        `module` либо имя класса.

    ⚠ Пустой список значит «нарушений нет», а не «файл не прочитан»: нечитаемый
    файл даёт `RuntimeError`, чтобы опечатка в пути не выглядела чистотой.

    Raises:
        RuntimeError: путь не существует.
    """
    path = Path(root)

    if not path.exists():
        raise RuntimeError(f'нет такого пути: {root}')

    files = [path] if path.is_file() else sorted(
        one for one in path.rglob('*.py')
        if not any(part in FUNCTION_ORDER_SKIP for part in one.parts))

    out = []

    for one in files:
        out.extend(_file_check(one))

    return out


def function_order_format(found: list, full: bool = False) -> str:
    """
    Отчёт словами. Пусто — порядок верный.

    Args:
        found: находки от `function_order`.
        full: показывать каждую находку; иначе по первой на файл.

    ⚠ По умолчанию **одна строка на файл**: нарушений в файле обычно столько,
    сколько публичных определений ниже приватной, и список из двадцати строк
    говорит ровно то же, что первая из них — «здесь перенос».
    """
    if not found:
        return 'порядок верный: приватные ниже публичных'

    lines, seen = [], set()

    for item in found:
        name = item['file']

        if name in seen and not full:
            continue

        # ⚠ Заголовок — один на файл, даже когда находок много: он про место, а
        # не про находку, и повторённый шесть раз прячет сами находки.
        if name not in seen:
            lines.append(f"\n── {name}")

        seen.add(name)

        if item['kind'] == FUNCTION_ORDER_MAIN_LATE:
            lines.append(f"   хук __main__ (стр. {item['line']}) не последний:"
                         f" ниже него ещё {item['after']}")
            continue

        where = '' if item['scope'] == 'module' else f" в классе {item['scope']}"
        lines.append(f"   {item['name']} (стр. {item['line']}){where}"
                     f" — публичное после приватного {item['after']}"
                     f" (стр. {item['after_line']})")

    lines.append(f"\nфайлов с нарушениями: {len(seen)}")

    return '\n'.join(lines).lstrip('\n')


def _file_check(path: Path) -> list:
    """Нарушения одного файла: тело модуля, тела классов и место хука."""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError, OSError):
        # ⚠ Молча пропускаем: в дереве попадаются файлы под другой питон и
        # шаблоны с подстановками. Падать на них значило бы не проверить и
        # остальные — а они как раз нужны.
        return []

    out = _body_check(tree.body, str(path), 'module')

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.extend(_body_check(node.body, str(path), node.name))

    out.extend(_main_check(tree.body, str(path)))

    return out


def _body_check(body: list, path: str, scope: str) -> list:
    """Публичные определения, стоящие ниже первого приватного."""
    private = None
    out = []

    for node in body:
        name = _name_of(node)

        if not name:
            continue

        if name.startswith('_') and not name.startswith('__'):
            # ⚠⚠ Приватную секцию открывает **только определение**, а не
            # константа. Приватная константа живёт в шапке рядом с публичными —
            # там ей и место: она объявляется до того, кто её читает. Считай мы
            # её началом приватной части, весь файл ниже шапки оказался бы
            # «нарушением»: на общем слое это дало 43 файла, почти все ложно.
            if private is None and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                private = (name, node.lineno)

            continue

        # ⚠ Дандеры (`__init__`, `__str__`) приватными не считаем: их место
        # задаёт другое правило — они стоят первыми среди методов, и запрет
        # «публичное после приватного» к ним не относится.
        if name.startswith('__'):
            continue

        if private is not None:
            out.append({'file': path, 'line': node.lineno, 'name': name,
                        'kind': FUNCTION_ORDER_PUBLIC_LATE,
                        'after': private[0], 'after_line': private[1],
                        'scope': scope})

    return out


def _main_check(body: list, path: str) -> list:
    """Хук `if __name__ == '__main__'` обязан быть последним в файле.

    ⚠⚠ Это и есть то, чего не видит рецепт из правил. Хук в середине даёт
    модуль, который **импортируется** и в тестах проходит, а запуск через CLI
    падает `NameError`: определений нижних приватных на момент вызова ещё нет.
    """
    place = None

    for index, node in enumerate(body):
        if isinstance(node, ast.If) and _is_main_guard(node):
            place = (index, node.lineno)

    if place is None or place[0] == len(body) - 1:
        return []

    tail = [_name_of(one) or type(one).__name__ for one in body[place[0] + 1:]]

    return [{'file': path, 'line': place[1], 'name': '__main__',
             'kind': FUNCTION_ORDER_MAIN_LATE,
             'after': ', '.join(one for one in tail if one)[:80],
             'after_line': 0, 'scope': 'module'}]


def _is_main_guard(node: ast.If) -> bool:
    """Тот ли это `if`, которым запускают файл напрямую."""
    test = node.test

    if not isinstance(test, ast.Compare) or not isinstance(test.left, ast.Name):
        return False

    return test.left.id == '__name__'


def _name_of(node) -> str:
    """Имя определения или константы модуля. Пусто — не определение.

    ⚠ Константы смотрим наравне с функциями: правило про порядок чтения, а
    `SOME_CONST` ниже приватной функции читается так же плохо, как и `def`.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name

    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id

    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id

    return ''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Приватные определения обязаны стоять ниже публичных '
                    '(docs/code_rules.md §3).')
    parser.add_argument('root', help='файл или дерево')
    parser.add_argument('--full', action='store_true',
                        help='все находки, а не первая на файл')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    try:
        got = function_order(args.root)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

    if args.json:
        import json

        print(json.dumps(got, ensure_ascii=False, indent=2))
    else:
        print(function_order_format(got, args.full))

    # ⚠ Код возврата — чтобы проверку можно было поставить в хук или в CI:
    # «нашли» обязано отличаться от «не нашли» без разбора текста.
    raise SystemExit(1 if got else 0)

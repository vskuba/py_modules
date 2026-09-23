"""Локальная переменная, прочитанная РАНЬШЕ первого присваивания: UnboundLocalError до запуска.

Питон решает судьбу имени по всей функции сразу: присвоил где угодно в теле — имя
локальное ВЕЗДЕ, включая строки выше присваивания. Поэтому такой код разбирается,
импортируется и живёт в репозитории, а падает лишь тогда, когда до строки дойдёт
исполнение:

    def collect(p):
        refs = pick(p, seed)          # UnboundLocalError: seed ещё не связан
        ...
        seed = p['seed'] or random()  # ...а связывается сотней строк ниже

Цена этого — прогон. В PersonaAI так упала работа №745: правка выглядела рабочей,
сюита её не трогала (функция ходит на ферму), а сторож свободных имён промолчал —
`seed` в функции связан, просто ниже по тексту. Он ищет имена, не связанные НИГДЕ,
и про порядок ничего не знает; здесь же ровно про порядок.

⚠ **Инструмент обязан молчать там, где кода нет ошибки** — иначе его перестанут
читать, и настоящая находка утонет среди ложных. Поэтому разобраны все области
видимости, а не только текст:

* **comprehension — своя область.** `{k: v for k, v in x.items()}` читает `k` в
  строке, где `k` ещё «не присвоен» по тексту; для питона это другая функция.
  Наивная проверка по номерам строк даёт тут ложную тревогу первой же;
* **вложенная функция и lambda исполняются ПОТОМ.** Чтение внутри них — не чтение
  сейчас, и к порядку внешней функции отношения не имеет;
* **тело класса исполняется СРАЗУ** — его чтения считаются, а присваивания в нём
  внешнюю функцию не связывают;
* **`global`/`nonlocal`** снимают локальность: такое имя не наше дело;
* **параметры** связаны входом в функцию, до любой строки.

Связывают имя не только `=`: `for`, `with ... as`, `except ... as`, `import`,
`:=`, `def`, `class`, `match ... as` и дополненное присваивание. Пропусти любую из
форм — и получишь ложную тревогу на ровном месте, поэтому они перечислены явно.

⚠ Чего инструмент НЕ ловит: ветку, где присваивание идёт по условию
(`if c: x = 1` … `print(x)`), и `del`. Там ошибка зависит от данных, а не от
текста, и сказать о ней статически нечего — молчание честнее догадки.
"""
import argparse
import ast
import sys

from pathlib import Path

# Что считаем исходником.
VARIABLE_UNBOUND_GLOB = '*.py'

# Куда не ходим: чужое, собранное и кэш.
VARIABLE_UNBOUND_SKIP = ('__pycache__', '.git', '.venv', 'venv', 'node_modules',
                         'build', 'dist', 'migrations')


def variable_unbound(root, skip: tuple = VARIABLE_UNBOUND_SKIP) -> list:
    """Чтения локальных имён до первого присваивания; пусто — таких нет.

    Args:
        root: файл или дерево с исходниками.
        skip: имена каталогов, внутрь которых не заходим.

    Returns:
        list: `[{'file', 'func', 'name', 'read', 'bound'}]` — где прочитано
        (`read`) и где связано (`bound`), номера строк.
    """
    where = Path(root)
    files = ([where] if where.is_file()
             else [p for p in sorted(where.rglob(VARIABLE_UNBOUND_GLOB))
                   if not any(part in skip for part in p.parts)])
    out = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as err:
            out.append({'file': str(path), 'func': '', 'name': '',
                        'read': getattr(err, 'lineno', 0) or 0, 'bound': 0,
                        'why': f'файл не разбирается: {err}'})
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out += _scope(str(path), node)
    return out


def _scope(file: str, fn) -> list:
    """Одна функция: имена, чьё первое чтение выше первого связывания."""
    declared = set()                  # global/nonlocal — локальность снята
    binds: dict = {}                  # имя -> первая строка связывания
    reads: dict = {}                  # имя -> первая строка чтения

    def bind(name, line):
        if name:
            binds[name] = min(binds.get(name, line), line)

    def read(name, line):
        if name:
            reads[name] = min(reads.get(name, line), line)

    for arg in _args(fn):
        bind(arg, fn.lineno)

    for node in _walk(fn.body):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, ast.Name):
            (bind if isinstance(node.ctx, ast.Store) else read)(node.id, node.lineno)
        elif isinstance(node, ast.ExceptHandler):
            bind(node.name, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bind(alias.asname or alias.name.split('.')[0], node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, node.lineno)
        elif isinstance(node, ast.MatchAs) and node.name:
            bind(node.name, node.lineno)
        elif isinstance(node, ast.MatchStar) and node.name:
            bind(node.name, node.lineno)

    return [{'file': file, 'func': fn.name, 'name': name,
             'read': reads[name], 'bound': binds[name]}
            for name in sorted(binds)
            if name not in declared and name in reads and reads[name] < binds[name]]


def _args(fn) -> list:
    """Имена параметров: связаны входом в функцию, до любой её строки."""
    a = fn.args
    got = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    return got + [x.arg for x in (a.vararg, a.kwarg) if x]


def _walk(body) -> list:
    """Узлы ТЕЛА функции, минус чужие области видимости.

    Вложенная функция и lambda исполняются позже — их чтения к порядку этой
    функции не относятся, и внутрь мы не идём (само имя `def` связывается
    снаружи, это делает вызывающий). У comprehension область своя: её цели —
    не наши имена, и считать их здесь значит выдумать ошибку. А вот ЧТЕНИЯ
    внутри comprehension наши: она исполняется той же строкой.

    Тело класса — наоборот: исполняется сразу, поэтому чтения из него берём, а
    присваивания в нём внешнюю функцию не связывают (они живут в классе).
    """
    out, stack = [], list(body)
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue                  # исполнится потом — не наш порядок
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            out += _comprehension(node)
            continue
        if isinstance(node, ast.ClassDef):
            out += [x for x in ast.walk(node)
                    if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)]
            continue
        stack += list(ast.iter_child_nodes(node))
    return out


def _comprehension(node, outer=()) -> list:
    """Чтения comprehension, кроме целей — её собственных и вложенных в неё.

    Цели (`for k, v in …`) живут в её области и снаружи не существуют. Без
    этого отсева первая же строка вида `{k: v for k, v in x.items()}` даёт
    ложную тревогу: по тексту `k` читается там же, где связывается.

    ⚠ Рекурсия обязательна: comprehension вкладывают друг в друга, и цель
    ВНУТРЕННЕЙ так же невидима снаружи. Плоский отсев (только свои цели) уже
    соврал на живом коде — `[max(t.height for t in r) for r in rows]`
    (`image_frames.py`): `t` принадлежит вложенному генератору, а инструмент
    считал его чтением внешней функции и обвинял строку ниже, где `t` честно
    заводится циклом.
    """
    own = set(outer)
    for gen in node.generators:
        own.update(x.id for x in ast.walk(gen.target)
                   if isinstance(x, ast.Name))
    # Цели не обходим вовсе: они связывают, а не читают.
    stack = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.comprehension):
            stack += [child.iter, *child.ifs]
        else:
            stack.append(child)
    out = []
    while stack:
        item = stack.pop()
        if isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            out += _comprehension(item, own)
            continue
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue                  # исполнится потом — не наш порядок
        if (isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
                and item.id not in own):
            out.append(item)
        stack += list(ast.iter_child_nodes(item))
    return out


def main() -> int:
    """CLI: `python -m variable_.variable_unbound <путь>`; код 1 — есть находки."""
    parser = argparse.ArgumentParser(
        description='Локальные имена, прочитанные раньше первого присваивания.')
    parser.add_argument('root', nargs='?', default='.', help='файл или дерево')
    args = parser.parse_args()

    found = variable_unbound(args.root)
    for row in found:
        if row.get('why'):
            print(f"  ⚠ {row['file']}:{row['read']} — {row['why']}", file=sys.stderr)
            continue
        print(f"  ⚠ {row['file']}:{row['read']} {row['func']}(): «{row['name']}» "
              f"читается раньше, чем связывается (строка {row['bound']}) — "
              f'UnboundLocalError, когда исполнение дойдёт')
    print(f'имён до присваивания: {len(found)}')
    return 1 if found else 0


if __name__ == '__main__':
    raise SystemExit(main())

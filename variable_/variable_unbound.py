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

Второй разбор — «точное связывание» (`_branchy`): имя, связанное лишь в одной
ветке и читаемое после развилки. `if c: x = 1` … `print(x)` он ловит.

⚠ **Внутри цикла второй разбор МОЛЧИТ, и это не лень.** Тело цикла проходится
со всеми его связываниями сразу, потому что имя, связанное в конце тела, на
следующем витке уже есть. Строгий разбор (как на первом витке) проверен:
**420 находок в `py_modules` и 186 в `src`**, почти все ложные — обычный приём
«связал в одной ветке, прочитал в другой, а до чтения ветка всегда
отрабатывала» неотличим от ошибки без знания данных.

Цена этого предела известна поимённо: прогон №805 упал `UnboundLocalError` на
`cos_mid`, связанном в одной ветке ВНУТРИ цикла кадров, и этот инструмент его
НЕ ловит. Ловит соседние случаи — вне цикла (`_branchy`) и любой порядок строк
(основной разбор).

⚠ Не ловит также `del` и случай, когда одно и то же условие проверяется дважды,
а между проверками меняется: совпадение текста условия считается признаком
безопасности (`_under`).
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

    out = [{'file': file, 'func': fn.name, 'name': name,
            'read': reads[name], 'bound': binds[name]}
           for name in sorted(binds)
           if name not in declared and name in reads and reads[name] < binds[name]]
    seen = {row['name'] for row in out}
    return out + [row for row in _branchy(file, fn, set(binds) - declared,
                                          set(_args(fn)))
                  if row['name'] not in seen]


def _branchy(file: str, fn, local: set, ready: set) -> list:
    """Имена, связанные ЛИШЬ В ОДНОЙ ветке, но читаемые после развилки.

    Порядок строк такое не ловит: связывание стоит ВЫШЕ чтения, просто до него
    исполнение может не дойти. Цена пропуска взыскана — прогон №805 упал
    `UnboundLocalError` на `cos_mid`, который заводился только в ветке «этапов
    нет», а прогон шёл с этапами.

    Считается «точное связывание»: после `if` без `else` имя из тела НЕ
    считается связанным, после `if/else` — только связанное в ОБЕИХ ветках.
    Ветка, которая всегда выходит (`return`, `raise`, `continue`, `break`),
    ничего не требует: после неё исполнение сюда не вернётся.

    ⚠ Тело цикла проходится со ВСЕМИ его связываниями сразу. Иначе обычный
    приём `for ...: prev = cur` с чтением `prev` на следующем витке читался бы
    ошибкой. Ценой — внутри цикла этот разбор молчит.
    """
    out, seen = [], set()
    # Под каким условием имя связали и под каким читают. Совпали — чтение
    # безопасно: `if ok: free = …` ниже `… if ok else …` читает `free` только
    # тогда, когда та же ветка уже отработала. Разобрать это потоком нельзя —
    # два `if ok` для разбора независимы, — а глазами видно сразу.
    under = _under(fn)

    def look(node, bound):
        """Чтения ЭТОГО узла, не заходя в чужие области видимости."""
        for x in _walk([node]):
            if (isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)
                    and x.id in local and x.id not in bound
                    and x.id not in seen
                    and not (under['read'].get(id(x), set())
                             & under['bind'].get(x.id, set()))):
                seen.add(x.id)
                out.append({'file': file, 'func': fn.name, 'name': x.id,
                            'read': x.lineno, 'bound': 0})

    def binds_of(body) -> set:
        """Всё, что связывает этот кусок, без оглядки на ветви."""
        got = set()
        for x in _walk(body):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                got.add(x.id)
            elif isinstance(x, ast.ExceptHandler) and x.name:
                got.add(x.name)
            elif isinstance(x, (ast.Import, ast.ImportFrom)):
                got.update(a.asname or a.name.split('.')[0] for a in x.names)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                got.add(x.name)
            elif isinstance(x, (ast.MatchAs, ast.MatchStar)) and x.name:
                got.add(x.name)
        return got

    def walk(body, bound) -> tuple:
        """Пройти блок; вернуть `(связано после, всегда ли выходит)`."""
        bound = set(bound)
        for st in body:
            if isinstance(st, (ast.Return, ast.Raise)):
                look(st, bound)
                return bound, True
            if isinstance(st, (ast.Break, ast.Continue)):
                return bound, True
            if isinstance(st, ast.If):
                look(st.test, bound)
                # ⚠ Условие ИСПОЛНЯЕТСЯ всегда, значит его моржовое связывание
                # (`if (n := len(x)) > 2:`) действует и после развилки.
                bound |= binds_of([st.test])
                a, a_out = walk(st.body, bound)
                b, b_out = (walk(st.orelse, bound) if st.orelse
                            else (set(bound), False))
                if a_out and b_out:
                    return bound, True
                bound = b if a_out else (a if b_out else a & b)
                continue
            if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
                look(getattr(st, 'iter', None) or st.test, bound)
                # ⚠ ЦЕЛЬ цикла (`for i in ...`) живёт в `st.target`, а не в
                # теле: без неё `i` внутри тела читался бы несвязанным — три
                # ложные тревоги на четырёх законных образцах.
                # ⚠ Моржовое связывание в условии `while` действует и в теле,
                # и ПОСЛЕ цикла: условие вычисляется хотя бы раз. Без этого
                # `while chunk := f.read(...)` давал ложную тревогу на `chunk`.
                if isinstance(st, ast.While):
                    bound |= binds_of([st.test])
                    # ⚠ `while True:` покидают только через `break` или `raise`,
                    # а перед `break` имя обычно как раз и связывают. Считать
                    # тело необязательным тут значит обвинять рабочий приём.
                    if (isinstance(st.test, ast.Constant) and st.test.value
                            and not st.orelse):
                        bound |= binds_of(st.body)
                inside = binds_of(st.body) | binds_of(
                    [st.target] if isinstance(st, (ast.For, ast.AsyncFor)) else [])
                walk(st.body, bound | inside)
                if st.orelse:
                    walk(st.orelse, bound)
                continue          # тело могло не выполниться — ничего не добавляем
            if isinstance(st, ast.Try):
                # ⚠ В `except` попадают с ЛЮБОГО места `try`, поэтому связывания
                # `try` там не считаются. После всего — пересечение выходов.
                t, t_out = walk(st.body + st.orelse, bound)
                ends = [] if t_out else [t]
                # ⚠ Обработчику отдаём и связывания `try` — НАРОЧНО, хотя
                # строго говоря упасть могло и до них. Строгий разбор даёт
                # ложные тревоги на самом обычном приёме: имя заводится первой
                # строкой `try`, падает третья, а `except` имя читает. Такой
                # тревоги больше, чем настоящих находок, а сторож, которому не
                # верят, бесполезен.
                for h in st.handlers:
                    h_bound, h_out = walk(
                        h.body, bound | binds_of(st.body)
                        | ({h.name} if h.name else set()))
                    if not h_out:
                        ends.append(h_bound)
                bound = set.intersection(*ends) if ends else set(bound)
                bound, _ = walk(st.finalbody, bound)
                continue
            if isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    look(item.context_expr, bound)
                bound |= binds_of(st.items)
                bound, out_ = walk(st.body, bound)
                if out_:
                    return bound, True
                continue
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
                bound.add(st.name)    # тело исполнится позже — внутрь не идём
                continue
            look(st, bound)
            bound |= binds_of([st])
        return bound, False

    walk(fn.body, set(ready))
    return out


def _under(fn) -> dict:
    """Под какими условиями имена связывают и под какими читают.

    Возвращает `{'bind': {имя: {ключи условий}}, 'read': {id(узла): {ключи}}}`.
    Ключ условия — разбор его текста (`ast.dump`), то есть два РАЗНЫХ по месту
    `if ok:` дают один ключ. Это и нужно: связали под `ok`, читаем под `ok` —
    значит к моменту чтения ветка уже отработала.

    Считаются и `if`-оператор, и тернарный `a if ok else b`: во втором виде
    чтение и пряталось (`comfy_gen_free`, строка 462).

    ⚠ Совпадение текста условия — не доказательство, а признак. Между двумя
    `if ok:` значение `ok` может измениться, и тогда находка была настоящей.
    Молчим намеренно: доля таких случаев мала, а поток ложных тревог убивает
    доверие ко всему инструменту разом.
    """
    out = {'bind': {}, 'read': {}}

    def go(node, keys):
        if isinstance(node, ast.If):
            key = ast.dump(node.test)
            go(node.test, keys)
            for st in node.body:
                go(st, keys | {key})
            for st in node.orelse:
                go(st, keys)
            return
        if isinstance(node, ast.IfExp):
            key = ast.dump(node.test)
            go(node.test, keys)
            go(node.body, keys | {key})
            go(node.orelse, keys)
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                out['bind'].setdefault(node.id, set()).update(keys)
            else:
                out['read'][id(node)] = keys
            return
        for child in ast.iter_child_nodes(node):
            go(child, keys)

    for st in fn.body:
        go(st, frozenset())
    return out


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

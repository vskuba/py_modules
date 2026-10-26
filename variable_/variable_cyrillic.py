"""Кириллица в ИМЕНАХ кода: найти, не спутав с комментариями и строками.

Правило простое — имена латиницей, кириллица только в комментариях и текстах для
человека. Проверить его `grep`-ом нельзя: в этих проектах комментарии и сообщения
русские, и любой поиск по `[а-яё]` тонет в них с первой строки.

Отличить имя от текста умеет только разбор: `ast` видит `Name` и `arg`, а
содержимое строк и комментариев ему не видно вовсе. Отсюда этот модуль.

## ⚠⚠ `col_offset` — это БАЙТЫ, а не символы

Главная ловушка, и она стоила испорченного прохода по четырнадцати файлам.
`ast` отдаёт `col_offset` смещением в **UTF-8**, где кириллица занимает два
байта. На строке с русским комментарием или строкой позиция уезжает ровно на
число таких символов левее:

    имя = row['ключ']  # и комментарий по-русски
          ^^^ col_offset тут больше, чем индекс символа

Резать надо байты:

    raw = line.encode('utf-8')
    было = raw[col:col_end].decode('utf-8')

Поэтому находка несёт и `col` (байты, как у `ast`), и `col_char` (символы, как у
среза строки) — чтобы переименование не пришлось переоткрывать заново.

## ⚠⚠ Перед переименованием — проверка на занятость

Имя, которое кажется свободным, бывает занято в той же области, и питон об этом
молчит до исполнения. Дважды пойманное живьём:

* `лица` → `faces`, а `faces` был параметром: вышло `const faces = faces` (это
  было в JS, но в питоне выйдет тише — переменная просто затрётся);
* `ушло` → `sent`, а `sent` в той же функции уже держал результат круга. Тест
  сравнивал список с числом и падал на утверждении, которое до правки работало.

Оба раза правка выглядела механической и безопасной. Поэтому
`variable_cyrillic_taken` есть здесь, а не оставлено на внимательность.

## Чего модуль НЕ делает

**Не переименовывает.** Имя по смыслу подбирает человек: одно слово в разных
местах значит разное — `куда` бывает и адресом ссылки, и сдвигом прокрутки.
Механическая замена превратила бы это в одно слово на оба случая.

**Не смотрит в SQL внутри строк.** `SELECT ... AS сутки` — тоже код, и его тоже
надо чинить, но для `ast` это обычная строка. Ищется отдельно, глазами или
`grep 'AS [а-яё_]'`.

## Что добирает `--decls`

По умолчанию модуль смотрит только обращения (`ast.Name`) и параметры (`ast.arg`)
— это имена, которые можно починить на месте. Объявления же молчали: строка

    def перепиши(куда):          # аргумент найден, а имя функции — нет

давала чистый отчёт при кириллической функции, и так же проходили `class`,
`import ... как`, `global`/`nonlocal`, `except ... как` и `case ... as`. Между тем
именно объявление попадает в чужие файлы: переименовать обращение, оставив
объявление, — значит получить `NameError` у соседа.

`--decls` добавляет их к отчёту, с пометкой рода (`def`, `class`, `import`,
`global`, `except`, `match`). ⚠ У таких узлов `col_offset` указывает на ключевое
слово (`def`, `global`, `except`), а не на имя, так что позиция имени ищется в
строке как отдельное слово — иначе указатель уезжает влево ровно на длину слова.
"""
import argparse
import ast
import re
import sys

from pathlib import Path

# Что считаем исходником.
VARIABLE_CYRILLIC_GLOB = '*.py'

# Куда не ходим: чужое, собранное и кэш.
VARIABLE_CYRILLIC_SKIP = ('__pycache__', '.git', '.venv', 'venv', 'node_modules',
                          'build', 'dist', 'migrations')

# Признак кириллицы. ⚠ `ё` и `Ё` стоят отдельно: в диапазон `а-я` они не входят.
VARIABLE_CYRILLIC_RE = re.compile('[а-яёА-ЯЁ]')

# Род имени, у которого `col_offset` указывает на само имя; у прочих — на
# ключевое слово строки, и позиция имени ищется отдельно.
VARIABLE_CYRILLIC_OFFSET_KINDS = ('name', 'arg')

# Как род объявления подписан в отчёте.
VARIABLE_CYRILLIC_KINDS = {'name': 'обращение', 'arg': 'параметр', 'def': 'функция',
                           'class': 'класс', 'import': 'импорт',
                           'global': 'область', 'except': 'исключение',
                           'match': 'сопоставление'}


def variable_cyrillic(root, skip: tuple = VARIABLE_CYRILLIC_SKIP,
                      decls: bool = False) -> list:
    """Кириллические имена в коде; пусто — таких нет.

    Args:
        root: файл или дерево с исходниками.
        skip: имена каталогов, внутрь которых не заходим.
        decls: считать и объявления — `def`, `class`, `import ... as`,
            `global`/`nonlocal`, `except ... as`, `case ... as`. Без них
            модуль видит только обращения и параметры, и кириллическое
            объявление проходит чисто (подробности — в заголовке модуля).

    Returns:
        list: `[{'file', 'func', 'name', 'line', 'col', 'col_char', 'end_char',
        'kind'}]`. `col` — байты (как отдаёт `ast`), `col_char` и `end_char` —
        символы (как режется строка). Почему их двое — в заголовке модуля.
        `kind` — чем является имя: `name`, `arg`, `def`, `class`, `import`,
        `global`, `except`, `match`.

    ⚠ `func` — **ближайшая** функция, то есть область имени. Одно слово в разных
    функциях значит разное, и переименовывать их надо порознь.
    """
    where = Path(root)
    files = ([where] if where.is_file()
             else [p for p in sorted(where.rglob(VARIABLE_CYRILLIC_GLOB))
                   if not any(part in skip for part in p.parts)])
    out = []
    for path in files:
        try:
            src = path.read_text(encoding='utf-8')
            tree = ast.parse(src, filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as err:
            out.append({'file': str(path), 'func': '', 'name': '', 'kind': '',
                        'line': getattr(err, 'lineno', 0) or 0, 'col': 0,
                        'col_char': 0, 'end_char': 0,
                        'why': f'файл не разбирается: {err}'})
            continue

        lines = src.split('\n')
        for node, name, kind in _named(tree, decls=decls):
            if not VARIABLE_CYRILLIC_RE.search(name):
                continue

            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ''
            start = len(line.encode('utf-8')[:node.col_offset].decode(
                'utf-8', 'ignore'))
            if kind not in VARIABLE_CYRILLIC_OFFSET_KINDS:
                # `col_offset` объявления указывает на ключевое слово, а не на имя.
                start = _word_char(line, name, start)
            out.append({'file': str(path), 'func': _owner(tree, node),
                        'name': name, 'kind': kind, 'line': node.lineno,
                        'col': node.col_offset, 'col_char': start,
                        'end_char': start + len(name)})
    return out


def variable_cyrillic_taken(path, func: str, want: str) -> bool:
    """Занято ли имя `want` в области функции `func`. `True` — переименовывать нельзя.

    Args:
        path: файл с исходником.
        func: имя функции, в которой собираются переименовывать.
        want: новое имя, которое хотят дать.

    ⚠⚠ **Спрашивать обязательно, а не «на глаз».** Питон не ругается на затёртое
    имя: переменная просто получает чужое значение, и ошибка вылезает там, где её
    никто не связывает с переименованием. Оба случая, на которых это выучено, —
    в заголовке модуля.

    ⚠ Проверяется и объемлющая область: замыкание видит имена внешней функции, и
    затереть их так же легко.
    ⚠ Считаются и объявления: `def` или `class` с таким именем занимает область
    ровно так же, как параметр, — а без этого проверки имя казалось свободным.
    """
    tree = ast.parse(Path(path).read_text(encoding='utf-8'), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func:
            continue

        for _kid, name, _kind in _named(node, decls=True, skip_self=node):
            if name == want:
                return True

    # Верхний уровень модуля: имя оттуда видно внутри любой функции.
    return any(name == want for _kid, name, _kind in _named(tree, top_only=True,
                                                            decls=True))


# ── Приватное ────────────────────────────────────────────────────────────────


def _named(tree, top_only: bool = False, decls: bool = False, skip_self=None):
    """Узлы, несущие имя: обращения, параметры, а с `decls` — и объявления.

    ⚠ Без `decls` это `ast.Name` и `ast.arg`, а не всё подряд: имена функций и
    классов не трогаем — переименование объявления тянет за собой всех вызывающих,
    и это другая работа, которую нельзя делать заодно. Поэтому объявления за
    пределами проверки — вопрос отдельного прогона с `--decls`, а не случайность.

    `skip_self` — узел, чьё собственное имя считать не надо: так `def` не делает
    занятым имя самой функции, когда спрашивают о её области.
    """
    nodes = (tree.body if top_only else ast.walk(tree))
    for node in nodes:
        if node is skip_self:
            continue
        if isinstance(node, ast.Name):
            yield node, node.id, 'name'
        elif isinstance(node, ast.arg):
            yield node, node.arg, 'arg'
        elif top_only and isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target, target.id, 'name'
        elif decls:
            yield from _decl(node)


def _decl(node):
    """Объявления, связывающие имя в этом файле: род и само имя узла."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        yield node, node.name, 'def'
    elif isinstance(node, ast.ClassDef):
        yield node, node.name, 'class'
    elif isinstance(node, ast.alias):
        # Псевдоним связывает имя здесь; путь модуля без `as` — имя чужого файла.
        yield node, (node.asname or node.name), 'import'
    elif isinstance(node, ast.ImportFrom) and node.module:
        yield node, node.module, 'import'
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        for name in node.names:
            yield node, name, 'global'
    elif isinstance(node, ast.ExceptHandler) and node.name:
        yield node, node.name, 'except'
    elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        yield node, node.name, 'match'
    elif isinstance(node, ast.MatchMapping) and node.rest:
        yield node, node.rest, 'match'


def _word_char(line: str, name: str, after: int) -> int:
    """Где в строке стоит слово `name` (после `after`); не нашлось — `after`.

    Нужно потому, что у объявления `col_offset` показывает на ключевое слово, а
    имя идёт правее. Ищем целым словом: `err` внутри `ValueError` не считается.
    """
    for match in re.finditer(r'\b' + re.escape(name) + r'\b', line[after:]):
        return after + match.start()
    return after


def _owner(tree, want) -> str:
    """Ближайшая функция, в теле которой лежит узел. Вне функций — `<модуль>`."""
    best = ''
    best_line = -1

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, 'end_lineno', node.lineno)
        if node.lineno <= want.lineno <= end and node.lineno > best_line:
            best, best_line = node.name, node.lineno

    return best or '<модуль>'


def main() -> int:
    """CLI: `python -m variable_.variable_cyrillic <путь> [--decls]`; 1 — есть находки."""
    parser = argparse.ArgumentParser(
        description='Кириллица в именах кода (комментарии и строки не считаются).')
    parser.add_argument('root', nargs='?', default='.', help='файл или дерево')
    parser.add_argument('--decls', action='store_true',
                        help='смотреть и объявления: def, class, import as, '
                             'global/nonlocal, except as, case as')
    args = parser.parse_args()

    found = variable_cyrillic(args.root, decls=args.decls)
    seen = set()
    for row in found:
        if row.get('why'):
            print(f"  ⚠ {row['file']}:{row['line']} — {row['why']}", file=sys.stderr)
            continue

        key = (row['file'], row['func'], row['name'])
        if key in seen:
            continue
        seen.add(key)
        kind = ('' if row['kind'] in ('name', 'arg', '')
                else f" — {VARIABLE_CYRILLIC_KINDS.get(row['kind'], row['kind'])}")
        print(f"  ⚠ {row['file']}:{row['line']} [{row['func']}] «{row['name']}»{kind}")

    print(f'кириллических имён: {len(seen)} (мест: {len(found)})'
          + ('' if args.decls else '; с --decls видно и объявления'))
    return 1 if found else 0


if __name__ == '__main__':
    raise SystemExit(main())

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


def variable_cyrillic(root, skip: tuple = VARIABLE_CYRILLIC_SKIP) -> list:
    """Кириллические имена в коде; пусто — таких нет.

    Args:
        root: файл или дерево с исходниками.
        skip: имена каталогов, внутрь которых не заходим.

    Returns:
        list: `[{'file', 'func', 'name', 'line', 'col', 'col_char', 'end_char'}]`.
        `col` — байты (как отдаёт `ast`), `col_char` и `end_char` — символы (как
        режется строка). Почему их двое — в заголовке модуля.

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
            out.append({'file': str(path), 'func': '', 'name': '',
                        'line': getattr(err, 'lineno', 0) or 0, 'col': 0,
                        'col_char': 0, 'end_char': 0,
                        'why': f'файл не разбирается: {err}'})
            continue

        lines = src.split('\n')
        for node, name in _named(tree):
            if not VARIABLE_CYRILLIC_RE.search(name):
                continue

            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ''
            start = len(line.encode('utf-8')[:node.col_offset].decode(
                'utf-8', 'ignore'))
            out.append({'file': str(path), 'func': _owner(tree, node),
                        'name': name, 'line': node.lineno,
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
    """
    tree = ast.parse(Path(path).read_text(encoding='utf-8'), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func:
            continue

        for kid, name in _named(node):
            if name == want:
                return True

    # Верхний уровень модуля: имя оттуда видно внутри любой функции.
    return any(name == want for _kid, name in _named(tree, top_only=True))


# ── Приватное ────────────────────────────────────────────────────────────────


def _named(tree, top_only: bool = False):
    """Узлы, несущие имя: обращения к переменным и параметры.

    ⚠ `ast.Name` и `ast.arg`, а не всё подряд: имена функций и классов не трогаем
    — переименование объявления тянет за собой всех вызывающих, и это другая
    работа, которую нельзя делать заодно.
    """
    nodes = (tree.body if top_only else ast.walk(tree))
    for node in nodes:
        for item in ([node] if top_only else [node]):
            if isinstance(item, ast.Name):
                yield item, item.id
            elif isinstance(item, ast.arg):
                yield item, item.arg
            elif top_only and isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        yield target, target.id


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
    """CLI: `python -m variable_.variable_cyrillic <путь>`; код 1 — есть находки."""
    parser = argparse.ArgumentParser(
        description='Кириллица в именах кода (комментарии и строки не считаются).')
    parser.add_argument('root', nargs='?', default='.', help='файл или дерево')
    args = parser.parse_args()

    found = variable_cyrillic(args.root)
    seen = set()
    for row in found:
        if row.get('why'):
            print(f"  ⚠ {row['file']}:{row['line']} — {row['why']}", file=sys.stderr)
            continue

        key = (row['file'], row['func'], row['name'])
        if key in seen:
            continue
        seen.add(key)
        print(f"  ⚠ {row['file']}:{row['line']} [{row['func']}] «{row['name']}»")

    print(f'кириллических имён: {len(seen)} (мест: {len(found)})')
    return 1 if found else 0


if __name__ == '__main__':
    raise SystemExit(main())

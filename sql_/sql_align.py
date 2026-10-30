"""Сверка колонок INSERT/SELECT, разбросанных по коду: списки одним голосом.

Живой ритуал «добавить колонку в снимок» оставляет три места, где колонки
перечислены руками: список `INSERT INTO t (…)` в сохранителе, список `SELECT …`
в читателе и счёт `%s` в `VALUES`. Забыл одно — и тест, читающий кортеж по
индексу, молча сдвигается на соседнюю колонку: `row[17]` вместо счёта вдруг
читает пустой диапазон, и провал теста враньём про данные выглядит ровно так
же, как настоящий провал. Счётчик `%s` расходится с длиной списка — запрос
падает уже на живой базе.

## Что сверяется

- **внутри одного файла**, по имени таблицы: списки **равной длины** — множество
  колонок (пропала/лишняя) и порядок (переставили); длина списка `INSERT`
  против числа значений в `VALUES`;
- сверка текстом, не графом: запросы живут в строковых литералах, порою
  склеенных из кусков — `'INSERT INTO t (a, ' 'b) VALUES'`, поэтому текст файла
  сперва сшивается (`_joined`), а не разбирается деревом.

## ⚠ Что молча пропускается

- **SELECT с выражениями** (`COUNT(*)`, `MAX(x)`, алиасы, подзапросы) — это не
  список колонок, сверять нечего.
- **SELECT другой длины**: `SELECT at, price` из таблицы, куда сохранитель
  пишет пять колонок, — штатное чтение подзадачей, а не расхождение.
- запросы, собранные строкой в цикле или f-строкой с `{…}` внутри колонок:
  список не разобрать — молчание, не находка.
- **межфайльные** пары: один файл пишет, другой читает — это уже чужой
  договор, не этот модуль.

## ⚠ Это не проверка схемы

`mysql_schema` сверяет таблицы с миграциями; здесь сверяются **два списка
одной таблицы внутри кода**. Таблицы в базе могут вообще не быть — не помеха.
"""
import argparse
import re
import sys

from file_.file_walk import file_walk

# Котёл SQL внутри питоновских строк: кавычки и конкатенация срезаются до
# разбора, иначе `'…(a, ' 'b)…'` даст колонку `a` и огрызок `` `b``.
SQL_ALIGN_JOIN = re.compile(r"'\s*\+?\s*'|\"\s*\+?\s*\"|'\s*'", re.S)
SQL_ALIGN_INSERT = re.compile(
    r"\binsert\s+(?:ignore\s+|into\s+)[`'\"]?(\w+)[`'\"]?\s*\((.*?)\)\s*(?:on\s+duplicate\s+key[\s\S]{0,400}?)?values\s*\((.*?)\)",
    re.I)
SQL_ALIGN_SELECT = re.compile(r"\bselect\s+([\s\S]{1,4000}?)\s+from\s+[`'\"]?(\w+)", re.I)

# Колонка — голое имя; всё остальное в списке (функции, `*`, алиасы, `a.b`) —
# не список колонок, а выражения: сверять нечего.
SQL_ALIGN_BARE = re.compile(r'^\w+$')


def sql_align(root: str = '.', table: str = '') -> list[dict]:
    """Найти расхождение колонок и плейсхолдеров между запросами одного файла.

    Args:
        root: каталог-корень; судятся `.py` файлы.
        table: сверять только эту таблицу; пусто — все найденные.

    Returns:
        Находки `{'file', 'table', 'kind', 'detail'}`; `kind` — `missing`,
        `extra`, `order`, `placeholders`. Пустой список — списки согласованы.

    ⚠ Порядок сверяется между списками одного файла, а не со схемной: колонка,
    добавленная в конец таблицы, но вписанная в `INSERT` в середину, безопасна
    для именованного запроса и смертельна для чтения по индексу — вот это
    порядок и ловит.
    """
    findings: list[dict] = []
    for path in file_walk(root, suffixes=('.py',)):
        try:
            with open(path, encoding='utf-8') as handle:
                joined = _joined(handle.read())
        except (UnicodeDecodeError, OSError):
            continue
        inserts, selects = {}, {}
        for match in SQL_ALIGN_INSERT.finditer(joined):
            cols = _columns(match.group(2))
            if cols is None:
                continue
            name = match.group(1).lower()
            inserts.setdefault(name, []).append(cols)
            _count(findings, path, name, cols, match.group(3))
        for match in SQL_ALIGN_SELECT.finditer(joined):
            # Захват не должен тащить чужие select/from: жадный до первого
            # `from` захват подхватывает и подзапрос — такой список не список.
            if re.search(r'\b(?:select|from|join)\b', match.group(1), re.I):
                continue
            cols = _columns(match.group(1))
            if cols is not None:
                selects.setdefault(match.group(2).lower(), []).append(cols)
        for name, lists in inserts.items():
            if table and name != table.lower():
                continue
            findings += _compare(path, name, lists + selects.get(name, []))
    return findings


# ── детали реализации ─────────────────────────────────────────────────────────

def _joined(text: str) -> str:
    """Склеенные литералы — в один текст: SQL не обязан жить в одной строке."""
    return SQL_ALIGN_JOIN.sub(' ', text)


def _columns(chunk: str) -> list | None:
    """Список — в имена; выражение внутри — None (сверять нечего)."""
    items = [item.strip().strip('`').strip() for item in chunk.split(',')]
    items = [item for item in items if item]
    if not items or not all(SQL_ALIGN_BARE.match(item) for item in items):
        return None
    return items


def _count(findings: list, path: str, table: str, cols: list, values: str) -> None:
    """Длина списка против числа значений `VALUES` — рассогласование роняет запрос на базе."""
    tokens = [item.strip() for item in values.split(',') if item.strip()]
    if not tokens or any('(' in item for item in tokens):
        return  # функции с аргументами — считать нечего, делёж по запятой врёт
    if len(tokens) != len(cols):
        findings.append({'file': path, 'table': table, 'kind': 'placeholders',
                         'detail': f'колонок {len(cols)}, значений {len(tokens)}'})


def _compare(path: str, table: str, lists: list) -> list[dict]:
    """Списки одной таблицы равной длины — к первому: множества и порядок.

    Разная длина — штатное чтение подзадачей: `SELECT at, price FROM …` там же,
    где сохранитель пишет пять колонок. Сверять эти списки — значит кричать на
    каждый ридер половинных выборок.
    """
    out = []
    base = lists[0]
    for other in lists[1:]:
        if len(other) != len(base):
            continue
        gone = [name for name in base if name not in other]
        added = [name for name in other if name not in base]
        if gone:
            out.append({'file': path, 'table': table, 'kind': 'missing',
                        'detail': 'нет в списке: ' + ', '.join(gone)})
        if added:
            out.append({'file': path, 'table': table, 'kind': 'extra',
                        'detail': 'лишние в списке: ' + ', '.join(added)})
        if not gone and not added and base != other:
            out.append({'file': path, 'table': table, 'kind': 'order',
                        'detail': 'один и тот же набор, разный порядок'})
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Сверка списков колонок INSERT/SELECT в коде.')
    parser.add_argument('root', nargs='?', default='.', help='каталог-корень')
    parser.add_argument('--table', default='', help='сверять только эту таблицу')
    args = parser.parse_args()

    hits = sql_align(args.root, table=args.table)
    if not hits:
        print('списки согласованы')
    for hit in hits:
        print(f"{hit['file']}  {hit['table']}  {hit['kind']}: {hit['detail']}")
    sys.exit(1 if hits else 0)

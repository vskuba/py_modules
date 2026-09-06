"""
Разовый запрос к базе проекта — с готовым адресом, без пароля в командной строке.

Живая привычка выглядит так: `docker exec <контейнер> mysql -uroot -p<пароль> -e '…'`.
В ней плохо всё сразу — имя контейнера угадывается, пароль виден в `ps` и остаётся
в истории, ответ приходит рисунком псевдографикой, а ошибка доступа или синтаксиса
стоит целого круга модели.

Здесь учётные данные берутся из `.env` (`config`), адрес — из `mysql_host`
(снаружи docker он не тот, что внутри), а ответ отдаётся либо таблицей для
человека, либо JSON для разбора.

Запись отделена от чтения намеренно: `sql` выполняет только читающие запросы, а
`UPDATE`/`DELETE`/`DROP` требуют явного `--write`. Модель, которой поручили
«посмотреть в базе», не должна иметь возможности случайно её поправить, а человек
одно слово допишет.

Модуль синхронный и одноразовый: соединение открывается и закрывается на запрос.
Приложению нужен не он, а пул — `mysql_.mysql_get_db_async`.
"""
import argparse
import json
import sys
from pathlib import Path

# Файл запускают путём (`python3 py_modules/mysql_/mysql_query.py`). Тогда в путях
# первым лежит каталог файла, а в нём — `mysql_.py`; при поиске `import mysql_`
# обычный модуль побеждает пакет без `__init__.py`, и соседний файл «не пакет».
# Поэтому свой каталог из путей убираем, а `py_modules` ставим в начало.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymysql
import pymysql.cursors

from config.config import config_get
from mysql_.mysql_host import mysql_host_address

# Первое слово читающего запроса. Всё остальное меняет данные или схему и требует `--write`.
MYSQL_QUERY_READ_ONLY = ('select', 'show', 'describe', 'desc', 'explain', 'with')

# Предел ширины колонки при печати таблицей: длинный JSON в ячейке разносит вёрстку
# на весь экран, а прочитать в нём всё равно ничего нельзя — для этого есть `--format json`.
MYSQL_QUERY_CELL_WIDTH = 80


def mysql_query_run(sql: str, params: tuple = (), limit: int = 0) -> dict:
    """
    Выполнить один запрос и вернуть результат.

    Args:
        sql: текст запроса; подстановки — `%s`, значения в `params`.
        params: значения подстановок.
        limit: обрезать выдачу до N строк; 0 — вернуть всё.

    Returns:
        `{'columns': [имена], 'rows': [{колонка: значение}], 'affected': N}`.
        У читающего запроса `affected` равно числу строк, у пишущего `rows` пуст.

    Raises:
        RuntimeError: база недоступна или отвергла запрос.
    """
    host, port = mysql_host_address()
    try:
        connection = pymysql.connect(
            host=host, port=port,
            user=str(config_get('MYSQL_USER', 'developer')),
            password=str(config_get('MYSQL_PASSWORD', '')),
            database=str(config_get('MYSQL_DATABASE', '')),
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True, connect_timeout=10, read_timeout=120,
        )
    except pymysql.Error as err:
        raise RuntimeError(f"База {host}:{port} не пустила: {err}")

    try:
        with connection.cursor() as cursor:
            affected = cursor.execute(sql, params or None)
            rows = list(cursor.fetchall() or ()) if cursor.description else []
            columns = [item[0] for item in (cursor.description or ())]
    except pymysql.Error as err:
        raise RuntimeError(f"Запрос отвергнут: {err}")
    finally:
        connection.close()

    if limit:
        rows = rows[:limit]
    return {'columns': columns, 'rows': [_row_json_safe(row) for row in rows], 'affected': affected}


def mysql_query_read_only(sql: str) -> bool:
    """
    Читает ли запрос — и только читает.

    Проверяется первое слово каждого выражения: комментарии и пустые выражения
    пропускаются. Смысл — не пустить правку туда, где просили посмотреть.

    Args:
        sql: текст запроса, возможно из нескольких выражений через `;`.

    Returns:
        True, если каждое выражение начинается со слова из `MYSQL_QUERY_READ_ONLY`.
    """
    statements = [item.strip() for item in _comments_strip(sql).split(';')]
    words = [item.split(None, 1)[0].lower() for item in statements if item]
    return bool(words) and all(word in MYSQL_QUERY_READ_ONLY for word in words)


def mysql_query_format(result: dict, style: str = 'table') -> str:
    """
    Показать результат: `table` — человеку, `json` — разбору, `csv` — переносу.

    Args:
        result: то, что вернул `mysql_query_run`.
        style: `table`, `json` или `csv`.

    Returns:
        Готовый к печати текст.
    """
    rows, columns = result.get('rows') or [], result.get('columns') or []
    if style == 'json':
        return json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    if not columns:
        return f"строк изменено: {result.get('affected', 0)}"
    if style == 'csv':
        lines = [','.join(columns)]
        lines += [','.join(_csv_cell(row.get(name)) for name in columns) for row in rows]
        return '\n'.join(lines)

    cells = [[_cell(row.get(name)) for name in columns] for row in rows]
    widths = [max(len(columns[i]), *(len(row[i]) for row in cells)) if cells else len(columns[i])
              for i in range(len(columns))]
    out = ['  '.join(name.ljust(widths[i]) for i, name in enumerate(columns)),
           '  '.join('-' * width for width in widths)]
    out += ['  '.join(value.ljust(widths[i]) for i, value in enumerate(row)) for row in cells]
    out.append(f"(строк: {len(rows)})")
    return '\n'.join(out)


# ── детали реализации ──

def _row_json_safe(row: dict) -> dict:
    """Даты, `Decimal` и байты — к строке: иначе строку не сериализовать и не напечатать."""
    safe = {}
    for key, value in row.items():
        if isinstance(value, bytes):
            safe[key] = value.decode('utf-8', 'replace')
        elif isinstance(value, (int, float, str, bool, type(None))):
            safe[key] = value
        else:
            safe[key] = str(value)
    return safe


def _comments_strip(sql: str) -> str:
    lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped.startswith('--') and not stripped.startswith('#'):
            lines.append(line)
    return '\n'.join(lines)


def _cell(value) -> str:
    text = '' if value is None else str(value)
    text = text.replace('\n', ' ').replace('\t', ' ')
    return text if len(text) <= MYSQL_QUERY_CELL_WIDTH else text[:MYSQL_QUERY_CELL_WIDTH - 1] + '…'


def _csv_cell(value) -> str:
    text = '' if value is None else str(value)
    return '"' + text.replace('"', '""') + '"' if {',', '"', '\n'} & set(text) else text


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Разовый запрос к базе проекта.')
    parser.add_argument('command', choices=['sql', 'tables', 'columns', 'address'],
                        help='sql — выполнить запрос; tables — список таблиц; '
                             'columns — колонки таблицы; address — куда идёт подключение')
    parser.add_argument('argument', nargs='?', default='', help='текст запроса или имя таблицы')
    parser.add_argument('--write', action='store_true',
                        help='разрешить запрос, меняющий данные или схему')
    parser.add_argument('--format', default='table', choices=['table', 'json', 'csv'])
    parser.add_argument('--limit', type=int, default=0, help='обрезать выдачу до N строк')
    args = parser.parse_args()

    try:
        if args.command == 'address':
            host, port = mysql_host_address()
            print(f"{host}:{port}  база={config_get('MYSQL_DATABASE', '')} "
                  f"пользователь={config_get('MYSQL_USER', '')}")
            raise SystemExit(0)

        if args.command == 'tables':
            query = 'SHOW TABLES' + (f" LIKE '{args.argument}'" if args.argument else '')
        elif args.command == 'columns':
            if not args.argument:
                raise SystemExit('ошибка: нужна таблица — `columns <таблица>`')
            query = f'DESCRIBE `{args.argument}`'
        else:
            query = args.argument or sys.stdin.read()
            if not query.strip():
                raise SystemExit('ошибка: пустой запрос')
            if not args.write and not mysql_query_read_only(query):
                raise SystemExit('ошибка: запрос меняет данные — повтори с --write')

        print(mysql_query_format(mysql_query_run(query, limit=args.limit), args.format))
    except RuntimeError as err:
        raise SystemExit(f"ошибка: {err}")

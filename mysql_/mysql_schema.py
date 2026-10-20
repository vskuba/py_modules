"""Разведка базы до запроса: где лежит нужная колонка и какие таблицы есть.

`Unknown column 'film' in cyber_order` и «таблицы cyber_customer не существует»
— каждый такой промах стоит запрос и минуту на догадку, как тут называются
данные. Схема базы в `information_schema` уже лежит, но запрос к ней каждый раз
пишется заново и обычно с ошибкой в кавычках.

Три вопроса, которые закрывают модуль: какие таблицы (по маске), что за колонки
в таблице, и — главное — в какой таблице вообще есть колонка/слово X. Всё на
штатном соединении `mysql_get_db_async`, схемой берёт ту, что в `MYSQL_DATABASE`.

Сверку двух баз делает `mysql_schema_diff`; здесь — взгляд на одну.
"""
import argparse
import asyncio
import json
import sys

from mysql_.mysql_ import mysql_get_db_async


async def mysql_schema_tables(mask: str = '*') -> list:
    """Таблицы текущей базы: имя, строк, комментарий.

    Args:
        mask: фильтр имени по маске, как в shell: `order*`, `*site*`.

    Returns:
        [{'table', 'rows', 'comment'}] — по алфавиту.
    """
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT table_name AS table_name, table_rows AS rows_est,'
            '       table_comment AS comment'
            '  FROM information_schema.tables'
            ' WHERE table_schema = DATABASE() AND table_name LIKE %s'
            ' ORDER BY table_name', (mask.replace('*', '%'),))
        rows = await db.fetchall() or []
    return [{'table': r['table_name'], 'rows': int(r['rows_est'] or 0),
             'comment': r['comment'] or ''} for r in rows]


async def mysql_schema_columns(table: str) -> list:
    """Колонки таблицы: имя, тип, nullable, ключ, описание.

    Raises:
        KeyError: таблицы нет в текущей базе — чтобы ответ «её нет» предшествовал
        запросу, а не следовал за ошибкой.
    """
    async with mysql_get_db_async() as db:
        await db.execute('SELECT 1 FROM information_schema.tables'
                         ' WHERE table_schema = DATABASE() AND table_name = %s',
                         (table,))
        if not await db.fetchone():
            raise KeyError(f'таблицы {table!r} в текущей базе нет —'
                           ' посмотри mysql_schema_tables')
        await db.execute(
            'SELECT column_name AS name, column_type AS type,'
            '       is_nullable AS nullable, column_key AS kv,'
            '       column_comment AS comment'
            '  FROM information_schema.columns'
            ' WHERE table_schema = DATABASE() AND table_name = %s'
            ' ORDER BY ordinal_position', (table,))
        rows = await db.fetchall() or []
    return [{'column': r['name'], 'type': r['type'],
             'nullable': r['nullable'] == 'YES', 'key': r['kv'] or '',
             'comment': r['comment'] or ''} for r in rows]


async def mysql_schema_find(needle: str) -> list:
    """Где в базе встречается слово: колонки и таблицы по подстроке.

    Отвечает на «в какой таблице лежит film» и на «таблиц с таким именем тут
    нет вообще» — пустой список и есть ответ второй половины вопроса.

    Returns:
        [{'table', 'column'}] — имя колонки или таблицы, подошедшие игле.
    """
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT table_name AS table_name, column_name AS column_name'
            '  FROM information_schema.columns'
            ' WHERE table_schema = DATABASE()'
            '   AND (column_name LIKE %s OR table_name LIKE %s)'
            ' ORDER BY table_name, column_name',
            (f'%{needle}%', f'%{needle}%'))
        rows = await db.fetchall() or []
    return [{'table': r['table_name'], 'column': r['column_name']}
            for r in rows]


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Разведка схемы: таблицы, колонки, где лежит слово.')
    sub = ap.add_subparsers(dest='mode', required=True)
    t = sub.add_parser('tables', help='какие таблицы (маска имени опциональна)')
    t.add_argument('mask', nargs='?', default='*')
    c = sub.add_parser('columns', help='колонки таблицы')
    c.add_argument('table')
    f = sub.add_parser('find', help='где встречается слово')
    f.add_argument('needle')
    ns = ap.parse_args()

    async def _main():
        if ns.mode == 'tables':
            for one in await mysql_schema_tables(ns.mask):
                print(f'{one["table"]:<40} {one["rows"]:>8}  {one["comment"]}')
            return 0
        if ns.mode == 'columns':
            try:
                for one in await mysql_schema_columns(ns.table):
                    star = ' *' if one['key'] else '  '
                    print(f'{star}{one["column"]:<28} {one["type"]:<16} '
                          f'{"NULL" if one["nullable"] else "NOT NULL":<9} '
                          f'{one["comment"]}')
            except KeyError as err:
                print(err, file=sys.stderr)
                return 1
            return 0
        hits = await mysql_schema_find(ns.needle)
        if not hits:
            print(f'слова {ns.needle!r} нет ни в колонках, ни в таблицах текущей базы')
            return 1
        for one in hits:
            print(json.dumps(one, ensure_ascii=False))
        return 0

    raise SystemExit(asyncio.run(_main()))

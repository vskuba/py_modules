"""Сторож строки базы: текущее значение колонки и готовая проба для watch'а.

Пока воркер учит модель часами, человек (и агент) живёт вопросами «дошло ли».
Вопрос этот — одна колонка одной строки, но каждый раз она выпрашивала один и
тот же shell: mysql-клиент в контейнере, пароль из `.env` аргументом, utf8mb4,
Cyrillic-ключи JSON в кавычках пути. Здесь — python-драйвер (пароль вообще не
светится в процессах) и готовая строка-проба: её можно отдать часовому сторожу
как есть, тот разбудит числом, когда значение сдвинется.

Значение и проба — всегда из одного запроса: промиллировать сторожа прежним
вопросом нельзя, и проба отдаётся собранной из тех же аргументов.
"""
import re

from mysql_.mysql_query import mysql_query_run

_IDENT = re.compile(r'^[A-Za-z0-9_]+$')


def state_watch(table, where: dict, column) -> dict:
    """Значение колонки строки сейчас + готовая строка-проба часовому сторожу.

    Args:
        table: имя таблицы (`photo_model`).
        where: {'колонка': 'значение'} — условия строки (`{'persona': 'diana'}`).
        column: колонка, за которой сторожить (`status`).

    Returns:
        {'value_of': значение или None — строки нет,
         'probe': строка `python -m mysql_.mysql_query sql …`, исполнимая в
         корне проекта часовым сторожем как есть}.
    """
    for name in [table, column, *where]:
        if not _IDENT.match(str(name)):
            raise ValueError(f'имя не латиницей с цифрами: {name!r}')
    conds = ' AND '.join(f'`{k}` = %s' for k in where)
    sql = f'SELECT `{column}` FROM `{table}` WHERE {conds} LIMIT 1'
    rows = mysql_query_run(sql, tuple(where.values()))['rows']
    shown = sql
    for v in where.values():
        shown = shown.replace('%s', "'" + str(v).replace("'", "''") + "'", 1)
    return {'value_of': rows[0][column] if rows else None,
            'probe': f"python -m mysql_.mysql_query sql \"{shown}\" "
                     f"--format json"}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='значение колонки строки базы сейчас + готовая проба '
                    'сторожу (--where повторять для сложных строк).')
    ap.add_argument('table', help='имя таблицы')
    ap.add_argument('column', help='колонка, за которой сторожить')
    ap.add_argument('--where', action='append', default=[], required=True,
                    metavar='колонка=значение', help='условие строки, повторять')
    ns = ap.parse_args()
    out = state_watch(ns.table, dict(w.split('=', 1) for w in ns.where),
                      ns.column)
    print(out['probe'])

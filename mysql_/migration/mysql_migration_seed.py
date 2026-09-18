"""Словарь домена → строки наполнения миграции: seed без дублей и оплошностей.

Новая таблица с закрытым словарём приезжала миграцией с INSERT-ами, которые
раскладывали руками: генератор строк в одноразовом скрипте, дубль слова в
словаре («студия» дважды) превращался в дубль в базе, слипшаяся запятая — в
исправление на пустом месте, а счётчик странного происхождения («44 и 43»)
никто не сверял. Функция делает раскладку сама и честным счётчиком: строки по
порядку, дубли схлопываются в первое вхождение, кавычки удваиваются, `None`
становится NULL, а число строк видно в ответе — сколько было и сколько стало.
"""


def mysql_migration_seed(rows, table: str, *, column: str = 'слово') -> dict:
    """Расставить строки наполнения в текст `INSERT IGNORE` для миграции.

    Args:
        rows: строки наполнения: список скаляров (одна колонка `column`) или
            список словарей (колонки — ключи первой строки, порядок ключей и
            есть порядок колонок); дубли схлопываются в первое вхождение.
        table: имя таблицы.
        column: имя колонки для скалярных строк (со словарями не нужна).

    Returns:
        {'sql': текст INSERT IGNORE с VALUES-строками по порядку (пусто, если
        строк нет), 'было': int, 'стало': int, 'колонки': [имена]}:
        'было' − 'стало' — сколько дублей схлопнулось, это и есть сверка
        счётчика.
    """
    items = list(rows or [])
    cols = (list(items[0]) if items and isinstance(items[0], dict)
            else [column])
    seen, values = set(), []
    for row in items:
        data = row if isinstance(row, dict) else {column: row}
        key = tuple(data.get(c) for c in cols)
        if key in seen:
            continue
        seen.add(key)
        values.append('(' + ', '.join(_lit(data.get(c)) for c in cols) + ')')
    sql = (f'INSERT IGNORE INTO `{table}` ('
           + ', '.join(f'`{c}`' for c in cols) + ') VALUES\n'
           + ',\n'.join(values) + ';') if values else ''
    return {'sql': sql, 'было': len(items), 'стало': len(values),
            'колонки': cols}


def _lit(value) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace('\\', '\\\\').replace("'", "''") + "'"


if __name__ == '__main__':
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(
        description='строки наполнения (JSON со stdin) → INSERT IGNORE для '
                    'миграции; дубли схлопываются, счётчик строк в ответе.')
    ap.add_argument('table', help='имя таблицы')
    ap.add_argument('--column', default='слово', help='колонка для скаляров')
    ns = ap.parse_args()
    print(mysql_migration_seed(json.load(sys.stdin), ns.table, column=ns.column)['sql'])

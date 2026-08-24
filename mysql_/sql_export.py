"""Сериализация SQL: значение в литерал и дамп таблиц в текст.

`sql_export_value` превращает Python-значение в SQL-литерал, `sql_export_tables`
собирает текст дампа (DDL и строки таблица за таблицей). Экранирует драйвер
(`pymysql.converters.escape_string`), а не мы: своё экранирование рано или поздно
не учитывает какой-нибудь символ, и файл рвётся посередине.

Подключение сюда не приходит — вызывающий сам его создаёт (см. `backup_engine`)
и сам сжимает результат: функция возвращает текст, а не файл.
"""

import io
from datetime import datetime, timezone

import pymysql
import pymysql.converters


def sql_export_value(value) -> str:
    """Значение в SQL-литерал: NULL, bool в 1/0, bytes/строки через драйвер."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "'" + pymysql.converters.escape_string(value.decode('utf-8', errors='replace')) + "'"

    return "'" + pymysql.converters.escape_string(str(value)) + "'"


def sql_export_tables(connection, tables, database='') -> str:
    """Текст дампа списка таблиц: DDL и строки таблица за таблицей.

    `connection` — живое pymysql-соединение с dict-курсором (строки-словари),
    `tables` — список имён. Возвращает текст: сжатие в файл и закрытие соединения
    делает вызывающий. Триггеры и процедуры в нём не будут.
    """
    buffer = io.StringIO()
    buffer.write('-- Backup (pymysql fallback)\n')
    buffer.write(f'-- Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC\n')
    if database:
        buffer.write(f'-- Database: {database}\n')
    buffer.write('\n')
    buffer.write('SET NAMES utf8mb4;\n')
    buffer.write('SET FOREIGN_KEY_CHECKS = 0;\n\n')

    for table in tables:
        buffer.write(_sql_export_table(connection, table))

    buffer.write('SET FOREIGN_KEY_CHECKS = 1;\n')

    return buffer.getvalue()


def _sql_export_table(connection, table: str) -> str:
    """Одна таблица: DROP, CREATE и все строки одним INSERT. Пустая — только DDL."""
    with connection.cursor() as cursor:
        cursor.execute(f'SHOW CREATE TABLE `{table}`')
        create = list(cursor.fetchone().values())[1]

    lines = [f'-- TABLE: {table}\n', f'DROP TABLE IF EXISTS `{table}`;\n', create + ';\n\n']

    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * FROM `{table}`')
        rows = cursor.fetchall()

    if not rows:
        return '\n'.join(lines)

    columns = list(rows[0].keys())
    values = ['  (' + ', '.join(sql_export_value(row[c]) for c in columns) + ')' for row in rows]
    lines.append(f'INSERT INTO `{table}` ({", ".join(f"`{c}`" for c in columns)}) VALUES\n')
    lines.append(',\n'.join(values) + ';\n\n')

    return '\n'.join(lines)

"""Съёмка дампа и восстановление базы по параметрам подключения.

Подключение приходит аргументами — host/port/user/password/db, без привязки к
конкретной установке: проект сам тянет их из конфига и передаёт сюда.

Дамп сначала снимает штатной утилитой `mysqldump`; если её нет в образе —
запасным путём на самом pymysql (через `sql_export_tables`). Восстановление
подаёт дамп клиенту `mysql`: без клиента залить нечем, и это обнаруживается
ровно тогда, когда восстановление и требуется.

Пароль уходит в утилиту через `MYSQL_PWD`, а не аргументом: аргументы видны в
`ps` любому, кто зашёл в контейнер.
"""

import gzip
import os
import subprocess

import pymysql

from logging_.logging_ import logger_info
from mysql_.sql_export import sql_export_tables

# Снять дамп быстрее, чем залить его обратно: заливка проигрывает весь файл
# построчно и на живой базе идёт в разы дольше. Отсюда и разные потолки.
BACKUP_ENGINE_DUMP_TIMEOUT = 300
BACKUP_ENGINE_RESTORE_TIMEOUT = 600


def backup_engine_dump(path: str, host: str, port: int, user: str,
                       password: str, db: str) -> None:
    """Снимает дамп в файл: сначала штатной утилитой, при её отсутствии — драйвером."""
    try:
        _backup_engine_dump_mysqldump(path, host, port, user, password, db)
    except FileNotFoundError:
        logger_info('[backup_engine] mysqldump не найден — снимаем драйвером, без триггеров и процедур')
        _backup_engine_dump_pymysql(path, host, port, user, password, db)


def backup_engine_restore(path: str, host: str, port: int, user: str,
                          password: str, db: str) -> None:
    """Распаковывает дамп и подаёт клиенту MySQL на вход.

    Клиент обязателен: без него дамп снимается, а восстановиться из него нечем.
    """
    with gzip.open(path, 'rb') as archive:
        sql = archive.read()

    env = os.environ.copy()
    env['MYSQL_PWD'] = password

    try:
        result = subprocess.run(
            ['mysql', f'--host={host}', f'--port={port}', f'--user={user}',
             '--default-character-set=utf8mb4', db],
            input=sql, stderr=subprocess.PIPE, env=env, timeout=BACKUP_ENGINE_RESTORE_TIMEOUT)
    except FileNotFoundError:
        raise RuntimeError('клиента mysql нет в образе — залить дамп нечем')

    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace').strip())


def _backup_engine_dump_mysqldump(path: str, host: str, port: int, user: str,
                                  password: str, db: str) -> None:
    """Штатный путь. `--single-transaction` — чтобы снимок был согласованным и
    не запирал таблицы: панель в это время продолжает работать."""
    env = os.environ.copy()
    env['MYSQL_PWD'] = password

    result = subprocess.run(
        ['mysqldump', f'--host={host}', f'--port={port}', f'--user={user}',
         '--single-transaction', '--routines', '--triggers',
         '--default-character-set=utf8mb4', db],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=BACKUP_ENGINE_DUMP_TIMEOUT)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace').strip())

    with gzip.open(path, 'wb', compresslevel=6) as archive:
        archive.write(result.stdout)


def _backup_engine_dump_pymysql(path: str, host: str, port: int, user: str,
                                password: str, db: str) -> None:
    """Запасной путь: DDL и строки таблица за таблицей. Триггеров и процедур в нём
    не будет — полагаться на то, что их нет, в общем случае нельзя."""
    connection = pymysql.connect(
        host=host, port=port, user=user, password=password, db=db,
        cursorclass=pymysql.cursors.DictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SHOW TABLES')
            tables = [list(row.values())[0] for row in cursor.fetchall()]

        sql = sql_export_tables(connection, tables, db)

        with gzip.open(path, 'wb', compresslevel=6) as archive:
            archive.write(sql.encode('utf-8'))
    finally:
        connection.close()

import os
from yoyo import read_migrations, get_backend
from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_get_url


def mysql_migration_up():
    logger_info('Yoyo: миграции mysql старт')

    current_dir = os.path.dirname(os.path.abspath(__file__))
    migrations_path = os.path.abspath(os.path.join(current_dir, "..", "..", "..", "migrations"))

    backend = get_backend(mysql_get_url())

    migrations = read_migrations(migrations_path)

    if not migrations:
        logger_info("Миграции не найдены")
        return

    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))

    logger_info('Yoyo: миграции mysql завершено')

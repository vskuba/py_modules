import os
from yoyo import read_migrations, get_backend
from logging_.logging_ import logger_info


def mysql_migration_up():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    migrations_path = os.path.abspath(os.path.join(current_dir, "..", "..", "migrations"))

    db_url = 'mysql://developer:password@mysql/project'
    backend = get_backend(db_url)

    migrations = read_migrations(migrations_path)

    if not migrations:
        logger_info("Миграции не найдены")
        return

    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))

    logger_info('Yoyo: Миграции применены успешно')

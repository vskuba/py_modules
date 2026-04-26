from mysql_migrations import MySQLMigrations


def mysql_migration_get() -> MySQLMigrations:
    # Инициализация объекта миграций
    # migration_file — это технический файл, где будет храниться номер текущей версии БД
    return MySQLMigrations(
        migration_dir='migrations',
        migration_file='.migration'
    )

import os
import pymysql

from urllib.parse import urlparse
from yoyo import read_migrations, get_backend

from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_get_url


def mysql_migration_up():
    mysql_url = mysql_get_url()

    parsed = urlparse(mysql_url)
    db_name = parsed.path.lstrip('/')

    logger_info(f'Yoyo: проверка наличия базы данных {db_name}...')

    try:
        conn = pymysql.connect(
            host=parsed.hostname or 'mysql',
            user=parsed.username or 'developer',
            password=parsed.password or 'password',
            port=parsed.port or 3306
        )
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.close()
    except Exception as e:
        logger_info(f"Yoyo: ошибка при создании базы: {e}")

    # 2. Теперь запускаем стандартный процесс yoyo
    logger_info('Yoyo: миграции mysql старт')

    current_dir = os.path.dirname(os.path.abspath(__file__))
    migrations_path = os.path.abspath(os.path.join(current_dir, "..", "..", "..", "migrations"))

    backend = get_backend(mysql_url)
    migrations = read_migrations(migrations_path)

    if not migrations:
        logger_info("Миграции не найдены")
        return

    # Получаем список миграций, которые еще не применены
    to_apply = backend.to_apply(migrations)

    if not to_apply:
        logger_info("Новых миграций для применения нет")
    else:
        logger_info(f"Будет применено миграций: {len(to_apply)}")
        # Выводим список файлов
        for m in to_apply:
            logger_info(f" - Применение: {os.path.basename(m.path)}")

        with backend.lock():
            backend.apply_migrations(to_apply)
            logger_info("Все миграции успешно применены")

    logger_info('Yoyo: миграции mysql завершено')

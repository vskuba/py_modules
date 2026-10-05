"""Наполнение пустой базы: применить последний seed-файл, если данных ещё нет.

Третий и последний способ доставить SQL в базу, и все три отвечают на разные
вопросы. `mysql_migration_up` двигает **схему** и помнит, что уже применено.
`backup_engine` снимает и возвращает **всё** состояние целиком. Seed — это
**стартовые данные**: справочники, роли, настройки, без которых свежая установка
не поднимется вообще.

Отсюда два свойства, которых нет у соседей:

* **применяется один раз и только на пустой базе.** Seed не помнит, что он делал
  (журнала применённого у него нет), поэтому повторный прогон на живой базе
  задвоил бы справочники. Проверка «база пуста» — единственное, что стоит между
  этим и рабочими данными;
* **берётся самый свежий файл по имени.** Seed-файлы копятся выгрузками
  (`seed_2026-08-17_22-20-17.sql`), и нужен последний — сортировка по имени
  работает, пока метка времени в начале имени.

⚠ **Чем проверять пустоту, знает только проект.** «База пуста» — это «в таблице
X ничего нет», а какая таблица говорит правду, зависит от схемы: у одного это
справочник узлов, у другого — пользователи. Поэтому `probe_table` обязателен и
умолчания не имеет: угаданное здесь имя означало бы, что на чужой схеме проверка
молча провалится в «база не пуста», и стартовые данные не приедут никогда.
"""

import os

import pymysql

from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_conn_get
from project_.project_ import project_root
from sql_.sql_split import sql_split_statements

# Где лежат seed-файлы по умолчанию — от корня проекта.
MYSQL_SEED_DIR = os.path.join('data', 'seeds')

MYSQL_SEED_SUFFIX = '.sql'


def mysql_seed_up(probe_table: str, seeds_dir: str = '') -> dict:
    """
    Применить последний seed-файл, если база пуста.

    Args:
        probe_table: таблица, по которой определяется пустота базы (см. докстринг
            модуля — умолчания у неё нет и быть не может).
        seeds_dir: каталог с `*.sql`. Пусто — `data/seeds` от корня проекта.

    Returns:
        `{'applied': bool, 'file': str, 'statements': int, 'reason': str}` —
        `reason` объясняет пропуск: файлов нет либо база не пуста.

    Raises:
        Exception: ошибка применения SQL. Транзакция откатывается целиком —
            полуприменённый seed хуже неприменённого: часть справочников есть, часть
            нет, и повторный прогон уже не пройдёт проверку на пустоту.
    """
    seed_file = _latest(seeds_dir)
    if not seed_file:
        logger_info(f'Seed: файлы не найдены в {_dir(seeds_dir)}, пропускаем')
        return {'applied': False, 'file': '', 'statements': 0, 'reason': 'нет файлов'}

    if not _fresh(probe_table):
        logger_info('Seed: база данных уже содержит данные, пропускаем')
        return {'applied': False, 'file': seed_file, 'statements': 0,
                'reason': 'база не пуста'}

    logger_info(f'Seed: свежая БД, применяем {os.path.basename(seed_file)}')

    with open(seed_file, 'r', encoding='utf-8') as f:
        statements = mysql_seed_split(f.read())

    conn = mysql_conn_get()
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        conn.commit()
        logger_info(f'Seed: успешно применён, запросов {len(statements)}')
    except Exception as e:
        conn.rollback()
        logger_info(f'Seed: ошибка применения — {e}')
        raise
    finally:
        conn.close()

    return {'applied': True, 'file': seed_file, 'statements': len(statements),
            'reason': ''}


def mysql_seed_split(sql: str) -> list[str]:
    """
    Разбить SQL-дамп на отдельные запросы по `;`.

    ⚠ Разбор живёт в `sql_/sql_split.py` — он один на дампы и на миграции. Здесь
    осталась дверь под прежним именем: её зовут два проекта, а переименование
    публичной функции слоя ломает соседей молча (`py_modules/CLAUDE.md`).

    ⚠⚠ Прежде разбор стоял здесь своей копией и комментарии **не разбирал**
    намеренно: «seed-файлы делает выгрузка, а не рука». Для выгрузки это правда,
    и расхождение вскрылось не на ней: у соседнего проекта та же задача решалась
    второй копией, умеющей комментарии, — и из 232 файлов миграций **136**
    разбирались двумя копиями по-разному.

    На seed-файлах переход ничего не менял: 12 файлов двух проектов, одно и то
    же число запросов, одно и то же тело — разница только в отброшенной шапке.
    """
    return sql_split_statements(sql)


def _dir(seeds_dir: str) -> str:
    """Каталог seed-файлов: переданный либо `data/seeds` от корня проекта.

    Копия такого же `_base` в `cronicle_/cronicle_log.py` осознанная —
    причина там же (`code_rules.md`, §4.2).
    """
    if seeds_dir:
        return os.path.abspath(seeds_dir)

    return os.path.join(str(project_root()), MYSQL_SEED_DIR)


def _latest(seeds_dir: str) -> str:
    """Самый свежий seed-файл по имени, либо пустая строка."""
    directory = _dir(seeds_dir)
    if not os.path.isdir(directory):
        return ''

    files = sorted((f for f in os.listdir(directory) if f.endswith(MYSQL_SEED_SUFFIX)),
                   reverse=True)

    return os.path.join(directory, files[0]) if files else ''


def _fresh(probe_table: str) -> bool:
    """
    Пуста ли база — по счёту строк в проверочной таблице.

    ⚠ **Ошибка запроса означает «не пуста», а не «пуста».** Таблицы может не быть
    вовсе (миграции ещё не прошли) или имя может быть опечатано — и в обоих
    случаях лить seed нельзя: на неготовую схему он не ляжет, а на живую базу с
    опечатанным именем задвоил бы справочники. Отказ в сторону бездействия здесь
    единственно верный.
    """
    if not str(probe_table or '').strip():
        raise ValueError('mysql_seed_up: probe_table обязателен — чем проверять '
                         'пустоту базы, знает только проект')

    conn = mysql_conn_get()
    try:
        with conn.cursor() as cur:
            # Имя таблицы приходит из кода проекта, а не от пользователя, но в
            # обратные кавычки берётся всё равно: имя вида `order` иначе упрётся
            # в зарезервированное слово.
            cur.execute(f'SELECT COUNT(*) FROM `{probe_table}`')
            row = cur.fetchone()
            count = list(row.values())[0] if isinstance(row, dict) else row[0]

        return count == 0
    except pymysql.Error as e:
        logger_info(f'Seed: проверка пустоты по `{probe_table}` не удалась — {e}')
        return False
    finally:
        conn.close()

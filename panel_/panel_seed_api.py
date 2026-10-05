"""Seed SQL: выгрузка выбранных таблиц в файл и обратная заливка.

Зачем он нужен. Схему накатывают миграции, а данные в свежей базе берутся
неоткуда: пользователи панели, справочники, привязки — всё это заводят руками.
Seed переносит их между установками одним файлом, не трогая схему.

⚠⚠ Чем отличается от бэкапа (`panel_backup_api.py`): seed — это **выбранные
таблицы** и `INSERT IGNORE`, то есть его можно применить поверх живой базы,
ничего не затерев. Бэкап — вся база целиком и полная перезапись; путать их дорого.

## ⚠ Три решения, которые тут важнее кода

* **выбор таблиц не хранится на сервере.** Заводить под него строку в настройках
  — платить схемой за галочку. Выбор помнит страница, а сервер каждый раз
  получает список явно, с запросом.
* **имена таблиц сверяются со списком базы.** Они попадают в **текст** запроса
  (параметром имя таблицы не передать), и брать их из тела запроса на веру
  значило бы отдать наружу выполнение произвольного SQL.
* **выгрузка идёт в отдельном потоке.** В цикле событий живут сокеты и фоновые
  задачи, и таблица на сотню тысяч строк, прочитанная в нём, оборвала бы их все.

⚠ Файлы лежат в `data/seeds` и в репозиторий не попадают: в них пароли учёток.
"""

import asyncio
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_conn_get
from mysql_.sql_export import sql_export_value
from pydantic import BaseModel, Field
from sql_.sql_split import sql_split_statements

from panel_.panel_auth import panel_auth_admin_required, panel_auth_need
from panel_.panel_file import (panel_file_dir, panel_file_info, panel_file_list,
                               panel_file_path, panel_file_stamp)

# Под каким путём живут точки.
PANEL_SEED_API_PREFIX = '/api/seed'

# Подкаталог в `data/` и расширение файла.
PANEL_SEED_SUB = 'seeds'
PANEL_SEED_EXT = '.sql'

# Таблицы, которых в списке нет вовсе. Это состояние миграций (`yoyo`): его ведёт
# сам мигратор, и залитое из чужой установки оно означает «схема уже накачена» на
# базе, где её нет.
PANEL_SEED_HIDDEN = ('_yoyo_log', '_yoyo_migration', '_yoyo_version', 'yoyo_lock')


class PanelSeedGenerate(BaseModel):
    """Какие таблицы выгружать."""

    tables: list[str] = Field(..., min_length=1,
                              description='Имена таблиц текущей базы')


def panel_seed_api_router(runtime_tables=(), title: str = '', view=None,
                          manage=None,
                          prefix: str = PANEL_SEED_API_PREFIX) -> APIRouter:
    """Роутер seed: `app.include_router(panel_seed_api_router(...))`.

    Args:
        runtime_tables: таблицы, которые в списке **есть, но не отмечены**. Их
            наполняет работа установки, а не человек: переписка, журналы,
            очереди. ⚠ Это единственное предметное знание здесь, и оно приходит
            доводом — скелет не вправе гадать, какие таблицы у проекта рабочие.
        title: как называть установку в шапке файла. Пусто — без имени.
        view: право на список таблиц, список файлов и скачивание.
        manage: право выгружать, применять и удалять.
        prefix: под каким путём жить.

    ⚠ Решает **сервер**, а не страница, какие таблицы отмечать: что заводят
    руками, а что копит сама работа, известно установке, а не браузеру.
    """
    runtime = tuple(runtime_tables or ())

    guard_view = panel_auth_need(view) if view else panel_auth_admin_required
    guard_manage = panel_auth_need(manage) if manage else panel_auth_admin_required

    router = APIRouter(prefix=prefix, tags=['seed'])

    @router.get('/table')
    async def seed_table_list(user=Depends(guard_view)):
        """Таблицы базы, сгруппированные по первому слову имени.

        Группы — не украшение: `user*` и `video*` это разные вещи, и выгружают
        их обычно порознь.
        """
        return {'result': await asyncio.to_thread(_groups, runtime)}

    @router.get('/file')
    async def seed_file_list(user=Depends(guard_view)):
        """Что уже выгружено. Свежие сверху — по имени, в нём дата."""
        return {'result': panel_file_list(_dir(), PANEL_SEED_EXT)}

    @router.post('/generate')
    async def seed_generate(data: PanelSeedGenerate, user=Depends(guard_manage)):
        """Выгружает выбранные таблицы в новый файл.

        ⚠⚠ Имена сверяются со списком базы **до** того, как попасть в запрос:
        чужого имени в списке нет, а значит и выполниться из него ничего не
        может.
        """
        groups = await asyncio.to_thread(_groups, runtime)
        known = {table['name'] for group in groups for table in group['tables']}
        tables = sorted({str(name).strip() for name in data.tables})

        unknown = [name for name in tables if name not in known]
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f'нет таких таблиц: {", ".join(unknown)}')

        directory = _dir()
        os.makedirs(directory, exist_ok=True)

        name = f'seed_{panel_file_stamp()}{PANEL_SEED_EXT}'
        path = os.path.join(directory, name)

        try:
            await asyncio.to_thread(_write, path, tables, title)
        except Exception as e:
            if os.path.exists(path):
                os.remove(path)
            logger_info(f'[seed] выгрузка не удалась: {type(e).__name__}: {e}')
            raise HTTPException(status_code=500, detail=f'выгрузка не удалась: {e}')

        logger_info(f'[seed] {user}: выгружен {name} ({", ".join(tables)})')

        return {'result': panel_file_info(directory, name)}

    @router.post('/apply/{name}')
    async def seed_apply(name: str, user=Depends(guard_manage)):
        """Заливает файл в текущую базу.

        `INSERT IGNORE` внутри файла: строки с занятыми ключами не трогаются, и
        повторное применение ничего не портит.
        """
        path = panel_file_path(_dir(), name, PANEL_SEED_EXT)

        try:
            await asyncio.to_thread(_apply, path)
        except Exception as e:
            logger_info(f'[seed] {name} не применился: {type(e).__name__}: {e}')
            raise HTTPException(status_code=500, detail=f'не применился: {e}')

        logger_info(f'[seed] {user}: применён {name}')

        return {'result': f'Seed {name} применён'}

    @router.get('/download/{name}')
    async def seed_download(name: str, user=Depends(guard_view)):
        """Отдаёт файл целиком. В нём пароли учёток — обращаться как с базой."""
        return FileResponse(panel_file_path(_dir(), name, PANEL_SEED_EXT),
                            filename=name, media_type='application/sql')

    @router.delete('/{name}')
    async def seed_delete(name: str, user=Depends(guard_manage)):
        """Удаляет файл. ⚠ Последний — тоже: это выгрузка, а не единственная
        копия данных, и держать её насильно незачем (в отличие от бэкапа)."""
        os.remove(panel_file_path(_dir(), name, PANEL_SEED_EXT))
        logger_info(f'[seed] {user}: удалён {name}')

        return {'result': 'удалено'}

    return router


# ── Приватное ────────────────────────────────────────────────────────────────

def _dir() -> str:
    """Каталог выгрузок. ⚠ Считается на вызове, а не на импорте."""
    return panel_file_dir(PANEL_SEED_SUB)


def _groups(runtime: tuple) -> list:
    """Таблицы базы по группам.

    ⚠ Читается из **схемы**, а не из списка в коде: список в коде расходится со
    схемой ровно в тот день, когда о нём забывают.
    """
    connection = mysql_conn_get()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES'
                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
                ' ORDER BY TABLE_NAME')
            tables = [str(row['TABLE_NAME']) for row in cursor.fetchall()]
    finally:
        connection.close()

    groups: dict = {}
    for table in tables:
        if table in PANEL_SEED_HIDDEN:
            continue
        groups.setdefault(table.split('_')[0], []).append(
            {'name': table, 'default': table not in runtime})

    return [{'key': key, 'label': key, 'tables': names}
            for key, names in sorted(groups.items())]


def _write(path: str, tables: list, title: str) -> None:
    """Пишет выгрузку. ⚠ Зовётся в отдельном потоке — внутри обычный pymysql."""
    mark = f' ({title})' if title else ''
    connection = mysql_conn_get()
    try:
        parts = [
            f'-- Seed SQL{mark}\n'
            f'-- Generated: '
            f'{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC\n'
            f'-- Tables: {", ".join(tables)}\n\n'
            'SET NAMES utf8mb4;\n'
            'SET FOREIGN_KEY_CHECKS = 0;\n'
        ]
        parts += [_table_sql(connection, table) for table in tables]
        parts.append('\nSET FOREIGN_KEY_CHECKS = 1;\n')

        with open(path, 'w', encoding='utf-8') as file:
            file.write('\n'.join(parts))
    finally:
        connection.close()


def _table_sql(connection, table: str) -> str:
    """Одна таблица одним `INSERT IGNORE`. Пустая — только заголовок."""
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * FROM `{table}`')
        rows = cursor.fetchall()

    lines = [f'\n-- TABLE: {table}']
    if not rows:
        lines.append('-- (пусто)')

        return '\n'.join(lines)

    columns = list(rows[0].keys())
    values = ['  (' + ', '.join(sql_export_value(row[column])
                                for column in columns) + ')' for row in rows]

    lines.append(f'INSERT IGNORE INTO `{table}` '
                 f'({", ".join(f"`{one}`" for one in columns)}) VALUES')
    lines.append(',\n'.join(values) + ';')

    return '\n'.join(lines)


def _apply(path: str) -> None:
    """Заливает файл.

    ⚠ Всё одной транзакцией: половина применённого seed хуже, чем
    неприменённый, — понять, что именно доехало, потом нечем.
    """
    with open(path, 'r', encoding='utf-8') as file:
        content = file.read()

    connection = mysql_conn_get()
    try:
        with connection.cursor() as cursor:
            for statement in sql_split_statements(content):
                cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager

from mysql_.mysql_ import mysql_get_db, mysql_get_db_async


class AbstractRepository(ABC):
    def __init__(self):
        self.table_name = self.table_name_get()

    @abstractmethod
    def table_name_get(self) -> str:
        pass

    async def find_one_by(self, criteria: dict, order_by: str = None):
        clause = " AND ".join([f"`{k}` = %s" for k in criteria.keys()])
        values = tuple(criteria.values())

        order_by_clause = ''
        if order_by:
            order_by_clause = f" ORDER BY {order_by}"

        sql = f"SELECT * FROM `{self.table_name}` WHERE {clause}{order_by_clause} LIMIT 1"

        async with asynccontextmanager(mysql_get_db_async)() as db:
            async with db.cursor() as cursor:
                await cursor.execute(sql, values)
                return await cursor.fetchone()

    def find_one_by_non_async(self, criteria: dict, order_by: str = None):
        clause = " AND ".join([f"`{k}` = %s" for k in criteria.keys()])
        values = tuple(criteria.values())

        order_by_clause = ''
        if order_by:
            order_by_clause = f" ORDER BY {order_by}"

        sql = f"SELECT * FROM `{self.table_name}` WHERE {clause}{order_by_clause} LIMIT 1"

        with next(mysql_get_db()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, values)
                return cursor.fetchone()

    async def find_by(self, criteria: dict, order_by: str = None):
        order_by_clause = ''
        if order_by:
            order_by_clause = f" ORDER BY {order_by}"

        if not criteria:
            sql = f"SELECT * FROM `{self.table_name}`{order_by_clause}"
            values = ()
        else:
            clause = " AND ".join([f"`{k}` = %s" for k in criteria.keys()])
            values = tuple(criteria.values())
            sql = f"SELECT * FROM `{self.table_name}` WHERE {clause}{order_by_clause}"

        async with asynccontextmanager(mysql_get_db_async)() as db:
            async with db.cursor() as cursor:
                await cursor.execute(sql, values)
                result = await cursor.fetchall()
                return result

    def find_by_non_async(self, criteria: dict, order_by: str = None):
        order_by_clause = ''
        if order_by:
            order_by_clause = f" ORDER BY {order_by}"

        if not criteria:
            sql = f"SELECT * FROM `{self.table_name}`{order_by_clause}"
            values = ()
        else:
            clause = " AND ".join([f"`{k}` = %s" for k in criteria.keys()])
            values = tuple(criteria.values())
            sql = f"SELECT * FROM `{self.table_name}` WHERE {clause}{order_by_clause}"

        with next(mysql_get_db()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, values)
                return cursor.fetchall()

    async def add(self, data: dict) -> int:
        """
        Создает новую запись в таблице.
        :param data: Словарь {'столбец': 'значение'}
        :return: ID созданной записи
        """
        if not data:
            raise ValueError("Данные для создания записи пусты")

        # 1. Формируем список столбцов: "`col1`, `col2`"
        columns = ", ".join([f"`{k}`" for k in data.keys()])

        # 2. Формируем заглушки: "%s, %s"
        placeholders = ", ".join(["%s"] * len(data))

        # 3. Собираем итоговый SQL
        sql = f"INSERT INTO `{self.table_name}` ({columns}) VALUES ({placeholders})"

        # 4. Получаем кортеж значений
        values = tuple(data.values())

        async with asynccontextmanager(mysql_get_db_async)() as db:
            async with db.cursor() as cursor:
                await cursor.execute(sql, values)
                return cursor.lastrowid

    async def add_many(self, data: list[dict]) -> int:
        """
        Массовая вставка данных в таблицу.
        :param data: Список словарей [{}, {}, ...]
        :return: Количество вставленных строк
        """
        if not data:
            return 0

        # 1. Берем ключи из первого словаря (считаем, что структура у всех одинаковая)
        keys = data[0].keys()
        columns = ", ".join([f"`{k}`" for k in keys])
        placeholders = ", ".join(["%s"] * len(keys))

        # 2. Формируем список кортежей значений для всех записей
        # Важно сохранить порядок полей как в переменной columns
        values = [tuple(item[k] for k in keys) for item in data]

        # 3. Собираем SQL
        sql = f"INSERT INTO `{self.table_name}` ({columns}) VALUES ({placeholders})"

        async with asynccontextmanager(mysql_get_db_async)() as db:
            async with db.cursor() as cursor:
                await cursor.executemany(sql, values)
                return cursor.rowcount

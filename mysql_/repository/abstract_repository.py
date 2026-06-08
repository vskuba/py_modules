from abc import ABC, abstractmethod

from mysql_.mysql_ import mysql_get_db, mysql_get_db_async


class AbstractRepository(ABC):
    def __init__(self):
        self.table_name = self.table_name_get()

    @abstractmethod
    def table_name_get(self) -> str:
        pass

    async def find(self, id: int):
        sql = f"SELECT * FROM `{self.table_name}` WHERE id = %s"
        async with mysql_get_db_async() as db:
            await db.execute(sql, id)
            return await db.fetchone()

    async def find_one_by(self, criteria: dict, order_by: str = None):
        clause = " AND ".join([f"`{k}` = %s" for k in criteria.keys()])
        values = tuple(criteria.values())

        order_by_clause = ''
        if order_by:
            order_by_clause = f" ORDER BY {order_by}"

        sql = f"SELECT * FROM `{self.table_name}` WHERE {clause}{order_by_clause} LIMIT 1"

        async with mysql_get_db_async() as db:
            await db.execute(sql, values)
            return await db.fetchone()

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

        async with mysql_get_db_async() as db:
            await db.execute(sql, values)
            result = await db.fetchall()
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

        async with mysql_get_db_async() as db:
            await db.execute(sql, values)
            return db.lastrowid

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

        async with mysql_get_db_async() as db:
            await db.executemany(sql, values)
            return db.rowcount

    async def delete(self, id: int) -> bool:
        """
        Удаляет запись по ID.
        """
        sql = f"DELETE FROM `{self.table_name}` WHERE id = %s"
        async with mysql_get_db_async() as db:
            await db.execute(sql, id)
            return db.rowcount > 0

    async def delete_by(self, criteria: dict):
        """
        Удаляет записи по заданным критериям.
        :param criteria: Словарь {'столбец': 'значение'}
        :return: Количество удаленных строк
        """
        if not criteria:
            raise ValueError("Критерии для удаления не могут быть пустыми")

        clause = " AND ".join([f"`{k}` = %s" for k in criteria.keys()])
        values = tuple(criteria.values())
        sql = f"DELETE FROM `{self.table_name}` WHERE {clause}"

        async with mysql_get_db_async() as db:
            await db.execute(sql, values)
            return db.rowcount

    async def update(self, id: int, data: dict) -> bool:
        """
        Обновляет запись по ID.
        :param id: ID записи
        :param data: Словарь с обновляемыми данными
        :return: True если запись обновлена
        """
        if not data:
            return False

        # Формируем строку SET: `col1` = %s, `col2` = %s
        set_clause = ", ".join([f"`{k}` = %s" for k in data.keys()])
        values = tuple(data.values()) + (id,)
        sql = f"UPDATE `{self.table_name}` SET {set_clause} WHERE id = %s"

        async with mysql_get_db_async() as db:
            await db.execute(sql, values)
            return db.rowcount > 0

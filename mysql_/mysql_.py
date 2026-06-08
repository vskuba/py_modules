import asyncio
import pymysql
import pymysql.cursors
import aiomysql

from typing import Optional
from aiomysql.pool import _create_pool, Pool

from config.config import config_get
from logging_.logging_ import logger_info

pool: Optional[aiomysql.Pool] = None
pool_lock = asyncio.Lock()

host = config_get('MYSQL_HOST', 'mysql')
user = config_get('MYSQL_USER', 'developer')
password = config_get('MYSQL_PASSWORD', 'password')
db = config_get('MYSQL_DATABASE', 'project')


class MySQLConnectionManager:
    """Собственная реализация асинхронного контекстного менеджера для БД"""

    def __init__(self):
        self.pool = None
        self.conn = None
        self.cursor_ctx = None
        # Инициализируем пустой строкой или None, но обрабатываем это в __getattr__
        self._cursor = None

    async def __aenter__(self):
        self.pool = await mysql_pool_get()
        self.conn = await self.pool.acquire()

        self.cursor_ctx = self.conn.cursor()
        self._cursor = await self.cursor_ctx.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.cursor_ctx:
                await self.cursor_ctx.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            if self.conn and self.pool:
                await self.pool.release(self.conn)

    def __getattr__(self, name):
        # Если курсор еще не создан (вызов до async with), отдаем понятное исключение
        if self._cursor is None:
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}'. "
                f"Убедитесь, что вы вызываете этот метод СТРОГО внутри блока 'async with db:'"
            )
        return getattr(self._cursor, name)

    async def execute(self, query, args=None):
        # Метод execute должен быть явно определен в классе (не через __getattr__)
        # Проверяем инициализацию перед выполнением
        if self._cursor is None:
            raise RuntimeError("Попытка выполнить execute() вне контекста 'async with db:'")

        log_msg = f"[MySQL SQL]: {query} | Args: {args}" if args else f"[MySQL SQL]: {query}"
        logger_info(log_msg)
        try:
            return await self._cursor.execute(query, args)
        except Exception as e:
            logger_info(f"[MySQL ERROR]: {str(e)} | Query: {query}")
            raise

    async def executemany(self, query, args):
        if self._cursor is None:
            raise RuntimeError("Попытка выполнить executemany() вне контекста 'async with db:'")

        logger_info(f"[MySQL SQL Many]: {query} | Count: {len(args)}")
        try:
            return await self._cursor.executemany(query, args)
        except Exception as e:
            logger_info(f"[MySQL ERROR Many]: {str(e)}")
            raise


class LoggingCursor:
    def __init__(self, cursor_ctx):
        self._ctx = cursor_ctx
        self._cursor = None

    async def __aenter__(self):
        # Входим в оригинальный контекстный менеджер aiomysql
        self._cursor = await self._ctx.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Выходим из него
        return await self._ctx.__aexit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    async def execute(self, query, args=None):
        log_msg = f"[MySQL SQL]: {query} | Args: {args}" if args else f"[MySQL SQL]: {query}"
        logger_info(log_msg)

        try:
            return await self._cursor.execute(query, args)
        except Exception as e:
            # Логируем ошибку, если запрос провалился
            logger_info(f"[MySQL ERROR]: {str(e)} | Query: {query}")
            raise  # Пробрасываем исключение дальше, чтобы оно обрабатывалось в приложении

    async def executemany(self, query, args):
        logger_info(f"[MySQL SQL Many]: {query} | Count: {len(args)}")
        try:
            return await self._cursor.executemany(query, args)
        except Exception as e:
            logger_info(f"[MySQL ERROR Many]: {str(e)}")
            raise


def mysql_get_url() -> str:
    return f'mysql://{user}:{password}@{host}/{db}'


def mysql_conn_get() -> pymysql.Connection:
    return pymysql.connect(
        host=host,
        user=user,
        password=password,
        database=db,
        cursorclass=pymysql.cursors.DictCursor,
        charset='utf8mb4'  # Рекомендуется для корректной работы с текстом/эмодзи
    )


def mysql_get_db():
    connection = mysql_conn_get()
    try:
        yield connection
    finally:
        connection.close()


def mysql_get_db_async():
    return MySQLConnectionManager()


async def mysql_pool_get() -> Pool:
    global pool
    if pool is None:
        async with pool_lock:
            loop = asyncio.get_running_loop()
            pool = await _create_pool(
                host=host,
                user=user,
                password=password,
                db=db,
                autocommit=True,
                cursorclass=aiomysql.DictCursor,
                minsize=5,
                maxsize=10,
                loop=loop
            )

    return pool


async def mysql_pool_close():
    global pool
    if pool is not None:
        pool.close()
        await pool.wait_closed()
        pool = None

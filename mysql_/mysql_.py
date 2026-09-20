import asyncio
import pymysql
import pymysql.cursors
import aiomysql

from typing import Optional
from aiomysql.pool import _create_pool, Pool

from config.config import config_get
from logging_.logging_ import logger_info
from mysql_.mysql_log import mysql_log_line

pool: Optional[aiomysql.Pool] = None
pool_lock = asyncio.Lock()

# Счетчик выданных соединений — только для диагностики зависаний пула (см. __aenter__/__aexit__).
# Позволяет по логам сопоставить acquire/release одного и того же чекаута и найти висящий "хвост".
_conn_seq = 0

# ⚠⚠ **Печать каждого запроса и каждого чекаута пула — трассировка отладки, и на
# проде она стоит гигабайтов.** Замерено на живом сервере: воркер писал 6832 строки
# в минуту, из них 6809 — эти две печати. Полтора гигабайта логов в сутки с одного
# контейнера, при полезных двадцати трёх строках в минуту.
#
# ⚠ **Умолчание — как было, «печатать».** Выключатель заводится в общей библиотеке,
# и молчаливая смена поведения застала бы врасплох соседние проекты: человек ищет
# запрос в журнале, а его там больше нет, и причина — в чужом коммите.
#
# Гасят их **порознь**, потому что отлаживают ими разное: `QUERIES` — что именно
# спросили у базы, `POOL` — куда делись соединения. Второе нужно редко и только
# при подозрении на утечку.
LOG_QUERIES = str(config_get('MYSQL_LOG_QUERIES', '1')).strip().lower() \
    not in ('0', 'false', 'no', 'off')
LOG_POOL = str(config_get('MYSQL_LOG_POOL', '1')).strip().lower() \
    not in ('0', 'false', 'no', 'off')

host = config_get('MYSQL_HOST', 'mysql')
port = int(config_get('MYSQL_PORT', '3306'))
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
        self._seq = None

    async def __aenter__(self):
        self.pool = await mysql_pool_get()
        self.conn = await self.pool.acquire()

        global _conn_seq
        _conn_seq += 1
        self._seq = _conn_seq
        if LOG_POOL:
            logger_info(f"[MySQL POOL] acquire #{self._seq}: used={len(self.pool._used)} free={self.pool.freesize} size={self.pool.size}")

        # Если что-то после acquire() упадет (в т.ч. asyncio.CancelledError при обрыве
        # запроса клиентом) — __aenter__ не вернет self, и Python НЕ вызовет __aexit__
        # (так работает async with: cleanup только при успешном enter). Без этого try
        # соединение навсегда остается в pool._used — видимый эффект: MySQL показывает
        # обычные "Sleep"-соединения, а пул приложения считает их занятыми навечно,
        # пока maxsize не исчерпается и все запросы к БД не начнут зависать на acquire().
        try:
            self.cursor_ctx = self.conn.cursor()
            self._cursor = await self.cursor_ctx.__aenter__()
        except BaseException:
            # ⚠ Эта печать **остаётся всегда**: она не про поток запросов, а про
            # сорвавшийся чекаут — то есть про беду, из-за которой пул течёт.
            logger_info(f"[MySQL POOL] acquire #{self._seq} failed before enter, releasing")
            await self.pool.release(self.conn)
            self.conn = None
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.cursor_ctx:
                await self.cursor_ctx.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            if self.conn and self.pool:
                await self.pool.release(self.conn)
                if LOG_POOL:
                    logger_info(f"[MySQL POOL] release #{self._seq}: used={len(self.pool._used)} free={self.pool.freesize} size={self.pool.size}")

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

        if LOG_QUERIES:
            logger_info(mysql_log_line(query, args))
        try:
            return await self._cursor.execute(query, args)
        except Exception as e:
            logger_info(f"[MySQL ERROR]: {str(e)} | Query: {query}")
            raise

    async def executemany(self, query, args):
        if self._cursor is None:
            raise RuntimeError("Попытка выполнить executemany() вне контекста 'async with db:'")

        if LOG_QUERIES:
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
        if LOG_QUERIES:
            logger_info(mysql_log_line(query, args))

        try:
            return await self._cursor.execute(query, args)
        except Exception as e:
            # Логируем ошибку, если запрос провалился
            logger_info(f"[MySQL ERROR]: {str(e)} | Query: {query}")
            raise  # Пробрасываем исключение дальше, чтобы оно обрабатывалось в приложении

    async def executemany(self, query, args):
        if LOG_QUERIES:
            logger_info(f"[MySQL SQL Many]: {query} | Count: {len(args)}")
        try:
            return await self._cursor.executemany(query, args)
        except Exception as e:
            logger_info(f"[MySQL ERROR Many]: {str(e)}")
            raise


def mysql_get_url() -> str:
    """Адрес базы строкой `mysql://…` — собирается из настроек окружения.

    ⚠ Содержит пароль открытым текстом: в журнал, в вывод команды и в текст ошибки
    не класть.
    """
    return f'mysql://{user}:{password}@{host}:{port}/{db}'


def mysql_conn_get() -> pymysql.Connection:
    """Отдельное синхронное соединение с базой — мимо пула, строки словарями.

    Returns:
        pymysql.Connection: новое соединение, `utf8mb4`, `DictCursor`.

    ⚠ Соединение **не из пула**, и закрывает его вызывающий. Асинхронному коду
    приложения нужен не этот вход, а `mysql_get_db_async`.
    """
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db,
        cursorclass=pymysql.cursors.DictCursor,
        charset='utf8mb4'  # Рекомендуется для корректной работы с текстом/эмодзи
    )


def mysql_get_db():
    """Синхронное соединение генератором — под зависимости FastAPI (`Depends`).

    Yields:
        pymysql.Connection: соединение; закрывается в `finally`, когда зависимость
        отработала.

    ⚠ Транзакцию за вызывающего не ведёт: `commit` остаётся на нём.
    """
    connection = mysql_conn_get()
    try:
        yield connection
    finally:
        connection.close()


def mysql_get_db_async():
    """Курсор из общего пула — обычный способ сходить в базу: `async with … as db`.

    Returns:
        MySQLConnectionManager: менеджер контекста; внутри — курсор со строками-
        словарями, соединение возвращается в пул на выходе.

    ⚠ Сама функция **не корутина**: `await` перед ней не нужен, работа начинается
    в `async with`.

    ⚠ Пул заводится с `autocommit=True` — отдельная транзакция не открывается,
    каждый запрос фиксируется сам по себе.
    """
    return MySQLConnectionManager()


async def mysql_pool_get() -> Pool:
    """Общий пул соединений с базой — один на процесс, заводится при первом обращении.

    Returns:
        Пул aiomysql: 5–10 соединений, autocommit, DictCursor.

    ⚠ Пул лежит в модульной переменной и привязан к event loop, в котором создан.
    Импорт длинной формой (`py_modules.mysql_.mysql_`) загрузит модуль второй раз —
    и пулов станет два.
    """
    global pool
    if pool is None:
        async with pool_lock:
            loop = asyncio.get_running_loop()
            pool = await _create_pool(
                host=host,
                port=port,
                user=user,
                password=password,
                db=db,
                autocommit=True,
                cursorclass=aiomysql.DictCursor,
                charset='utf8mb4',
                minsize=5,
                maxsize=10,
                loop=loop
            )

    return pool


async def mysql_pool_close():
    """Закрыть общий пул и дождаться, пока соединения отпустят, — на остановке приложения.

    ⚠ Обнуляет модульную переменную, поэтому следующий `mysql_pool_get` заведёт пул
    заново. В кронджобе это обязательный `finally`: без закрытия процесс не выйдет,
    пока соединения не отвалятся по таймауту.
    """
    global pool
    if pool is not None:
        pool.close()
        await pool.wait_closed()
        pool = None

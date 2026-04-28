import asyncio

import pymysql
import pymysql.cursors
import aiomysql

from typing import Optional
from aiomysql.pool import _create_pool, Pool
from contextlib import contextmanager, asynccontextmanager

pool: Optional[aiomysql.Pool] = None
pool_lock = asyncio.Lock()

host = 'mysql'
user = 'developer'
password = 'password'
db = 'project'


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


@contextmanager
def mysql_get_db():
    connection = mysql_conn_get()
    try:
        yield connection
    finally:
        connection.close()


@asynccontextmanager
async def mysql_get_db_async():
    p = await mysql_pool_get()
    async with p.acquire() as conn:
        yield conn


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

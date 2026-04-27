from mysql_.mysql_ import mysql_pool_get


async def memory_short_message_add(user_id, role, agent, content):
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            # Используем %s для MySQL
            sql = '''
                INSERT INTO memory_short (user_id, role, agent, content)
                VALUES (%s, %s, %s, %s)
            '''
            await cursor.execute(sql, (user_id, role, agent, content))
            # При autocommit=True в настройках пула, commit() произойдет автоматически


async def memory_short_messages(user_id, agent, limit=50) -> list:
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            sql = '''
                SELECT content FROM memory_short 
                WHERE user_id = %s AND agent = %s 
                ORDER BY id DESC 
                LIMIT %s
            '''
            await cursor.execute(sql, (user_id, agent, limit))
            rows = await cursor.fetchall()

            # Так как в пуле стоит DictCursor, row — это словарь
            # Переворачиваем, чтобы история шла от старых к новым
            return [row['content'] for row in reversed(rows)]
from mysql_.mysql_ import mysql_pool_get


async def memory_short_message_add(session_uuid, user_id, role, agent, kind_type, content):
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            # Используем %s для MySQL
            sql = '''
                INSERT INTO agent_memory_short (session_uuid, user_id, role, agent, kind_type, content)
                VALUES (%s, %s, %s, %s, %s, %s)
            '''
            await cursor.execute(sql, (session_uuid, user_id, role, agent, kind_type, content))
            # При autocommit=True в настройках пула, commit() произойдет автоматически


async def memory_short_messages(user_id, agent, limit=50) -> list:
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            sql = '''
                SELECT content FROM agent_memory_short 
                WHERE user_id = %s AND agent = %s 
                ORDER BY id DESC 
                LIMIT %s
            '''
            await cursor.execute(sql, (user_id, agent, limit))
            rows = await cursor.fetchall()

            # Так как в пуле стоит DictCursor, row — это словарь
            # Переворачиваем, чтобы история шла от старых к новым
            return [row['content'] for row in reversed(rows)]
import uuid

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


async def memory_short_messages(user_id, agent, limit=10) -> list:
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            sql = '''
                SELECT session_uuid, role, kind_type, content FROM agent_memory_short 
                WHERE user_id = %s AND agent = %s AND kind_type in ('user-prompt', 'response')
                ORDER BY created_at DESC 
                LIMIT %s
            '''
            await cursor.execute(sql, (user_id, agent, limit))
            rows = await cursor.fetchall()

            return list(rows)


async def memory_short_session_uuid_get(user_id, agent) -> str:
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            sql = '''
                SELECT session_uuid FROM agent_memory_short 
                WHERE user_id = %s AND agent = %s 
                ORDER BY created_at DESC 
                LIMIT 1
            '''
            await cursor.execute(sql, (user_id, agent))
            row = await cursor.fetchone()

            if not row:
                return str(uuid.uuid4())

            return row['session_uuid']

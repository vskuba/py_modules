import uuid

from mysql_.mysql_ import mysql_pool_get


async def session_message_add(session_uuid, llm_id, user_id, role, agent_id, kind_type, content, token=None):
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            # Используем %s для MySQL
            sql = '''
                INSERT INTO agent_session (session_uuid, llm_id, user_id, role, agent_id, kind_type, content, token)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            '''
            await cursor.execute(sql, (session_uuid, llm_id, user_id, role, agent_id, kind_type, content, token))
            # При autocommit=True в настройках пула, commit() произойдет автоматически


async def session_messages(user_id, agent_id, limit=10) -> list:
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            sql = '''
                SELECT session_uuid, role, kind_type, content FROM agent_session 
                WHERE user_id = %s AND agent_id = %s AND kind_type in ('user-prompt', 'response-final') 
                ORDER BY created_at DESC, id DESC
                LIMIT %s
            '''
            await cursor.execute(sql, (user_id, agent_id, limit))
            rows = await cursor.fetchall()

            return list(reversed(rows))


async def session_uuid_get(user_id, agent_id) -> str:
    pool = await mysql_pool_get()
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            sql = '''
                SELECT session_uuid FROM agent_session 
                WHERE user_id = %s AND agent_id = %s 
                ORDER BY created_at DESC 
                LIMIT 1
            '''
            await cursor.execute(sql, (user_id, agent_id))
            row = await cursor.fetchone()

            if not row:
                return str(uuid.uuid4())

            return row['session_uuid']

import uuid

from mysql_.mysql_ import mysql_get_db_async


async def ai_session_message_add(session_uuid, llm_id, user_id, role, agent_id, kind_type, content, token=None):
    async with mysql_get_db_async() as db:
        # Используем %s для MySQL
        sql = '''
            INSERT INTO agent_session (session_uuid, llm_id, user_id, role, agent_id, kind_type, content, token)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        '''
        await db.execute(sql, (session_uuid, llm_id, user_id, role, agent_id, kind_type, content, token))
        # При autocommit=True в настройках пула, commit() произойдет автоматически


async def ai_session_messages(user_id, agent_id, limit=10) -> list:
    async with mysql_get_db_async() as db:
        sql = '''
            SELECT session_uuid, role, kind_type, content FROM agent_session 
            WHERE user_id = %s AND agent_id = %s AND kind_type in ('user-prompt', 'response-final') 
            ORDER BY created_at DESC, id DESC
            LIMIT %s
        '''
        await db.execute(sql, (user_id, agent_id, limit))
        rows = await db.fetchall()

        return list(reversed(rows))


async def ai_session_uuid_get(user_id, agent_id) -> str:
    async with mysql_get_db_async() as db:
        sql = '''
            SELECT session_uuid FROM agent_session 
            WHERE user_id = %s AND agent_id = %s 
            ORDER BY created_at DESC 
            LIMIT 1
        '''
        await db.execute(sql, (user_id, agent_id))
        row = await db.fetchone()

        if not row:
            return str(uuid.uuid4())

        return row['session_uuid']


async def ai_session_metadata_get(session_uuid) -> dict:
    async with mysql_get_db_async() as db:
        sql = '''
            SELECT metadata FROM session WHERE uuid = %s
        '''
        await db.execute(sql, (session_uuid,))
        row = await db.fetchone()

        if not row:
            return {}

        return row['metadata'] or {}


async def ai_session_metadata_set(session_uuid, metadata: dict):
    async with mysql_get_db_async() as db:
        sql = '''
            UPDATE session SET metadata = %s WHERE uuid = %s
        '''
        await db.execute(sql, (metadata, session_uuid))

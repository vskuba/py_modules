from typing import Any

from mysql_.mysql_ import mysql_get_db_async


async def setting_get(key: str, default: Any | None = None, user_id: int | None = None) -> Any | None:
    async with mysql_get_db_async() as db:
        sql = "SELECT value FROM setting WHERE `key` = %s AND user_id "
        if user_id is None:
            sql += "IS NULL"
            params = (key,)
        else:
            sql += "= %s"
            params = (key, user_id)

        await db.execute(sql, params)
        result = await db.fetchone()

        if result:
            return result['value'] if isinstance(result, dict) else result[0]

        return default


async def setting_set(key: str, value, user_id: int | None = None) -> bool:
    async with mysql_get_db_async() as db:
        sql = """
            INSERT INTO setting (`key`, value, user_id) 
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE value = VALUES(value)
        """
        await db.execute(sql, (key, str(value), user_id))
        await db.commit()
    return True

from typing import Any

from mysql_.mysql_ import mysql_get_db_async


async def setting_get(
        key: str,
        default: Any | None = None,
        user_id: int | None = None,
        node_id: int | None = None
) -> Any | None:
    async with mysql_get_db_async() as db:
        condition, params = _scope_condition(user_id, node_id)
        await db.execute(
            f"SELECT value FROM setting WHERE `key` = %s AND {condition}",
            [key, *params]
        )
        result = await db.fetchone()

        if result:
            return result['value'] if isinstance(result, dict) else result[0]

        return default


async def setting_set(
        key: str,
        value,
        user_id: int | None = None,
        node_id: int | None = None
) -> bool:
    async with mysql_get_db_async() as db:
        # Уникальный индекс не ловит дубли при NULL в scope-колонках — свой upsert
        condition, params = _scope_condition(user_id, node_id)
        await db.execute(
            f"UPDATE setting SET value = %s WHERE `key` = %s AND {condition}",
            [str(value), key, *params]
        )
        if not db.rowcount:
            # rowcount 0 и при неизменившемся значении — проверяем наличие строки
            await db.execute(
                f"SELECT id FROM setting WHERE `key` = %s AND {condition}",
                [key, *params]
            )
            if not await db.fetchone():
                await db.execute(
                    "INSERT INTO setting (`key`, value, user_id, node_id) VALUES (%s, %s, %s, %s)",
                    (key, str(value), user_id, node_id)
                )
        await db.connection.commit()
    return True


async def setting_delete(
        key: str,
        user_id: int | None = None,
        node_id: int | None = None
) -> bool:
    async with mysql_get_db_async() as db:
        condition, params = _scope_condition(user_id, node_id)
        await db.execute(
            f"DELETE FROM setting WHERE `key` = %s AND {condition}",
            [key, *params]
        )
        await db.connection.commit()
    return True


def _scope_condition(user_id: int | None, node_id: int | None) -> tuple[str, list]:
    """Условие области настройки: пара (user_id, node_id), где None означает IS NULL."""
    conditions = []
    params: list = []

    if user_id is None:
        conditions.append("user_id IS NULL")
    else:
        conditions.append("user_id = %s")
        params.append(user_id)

    if node_id is None:
        conditions.append("node_id IS NULL")
    else:
        conditions.append("node_id = %s")
        params.append(node_id)

    return " AND ".join(conditions), params

from mysql_.mysql_ import mysql_get_db_async


async def mysql_table_metadata_get(table_name: str) -> dict:
    """
    Получает описание полей таблицы, парсит комментарии и возвращает словарь
    с метаданными для каждого поля.
    """
    result = {}
    query = f"SHOW FULL COLUMNS FROM `{table_name}`"

    try:
        async with mysql_get_db_async() as db:
            await db.execute(query)
            columns = await db.fetchall()

            for col in columns:
                field_name = col.get('Field')
                raw_comment = col.get('Comment') or ""

                # Парсинг метаданных из строки комментария
                metadata = {}
                if raw_comment:
                    # Разделяем по '|', затем по ':'
                    parts = raw_comment.split('|')
                    for part in parts:
                        if ':' in part:
                            key, val = part.split(':', 1)
                            metadata[key.strip()] = val.strip()

                result[field_name] = {
                    "comment": raw_comment,
                    "comment_metadata": metadata
                }
    except Exception as e:
        print(f"Ошибка при получении структуры таблицы {table_name}: {e}")
        return {}

    return result

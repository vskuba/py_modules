import os
import sqlite3

from config.config import config_get

db_path = os.path.join(
    config_get('data_dir'),
    config_get('sqllite3_dir'),
    'llm_short_memory.db'
)

os.makedirs(os.path.dirname(db_path), exist_ok=True)

conn = sqlite3.connect(db_path, check_same_thread=False)


def sqllite3_llm_short_memory_init():
    """Создать таблицу коротких сообщений, если её ещё нет.

    Отдельно звать не нужно: запись и чтение начинают с неё сами.

    ⚠ Соединение открывается **на импорте модуля**, с `check_same_thread=False` —
    одно на процесс и общее для всех потоков. Файл базы кладётся по `data_dir` из
    настроек, каталог создаётся тем же импортом.
    """
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,
            agent TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()


def sqllite3_llm_short_memory_message_add(user_id, role, agent, content):
    """Дописать реплику в короткую память агента.

    Args:
        user_id: с кем идёт разговор.
        role: чья реплика — роль в терминах модели.
        agent: какой агент помнит; память у каждого своя.
        content: текст реплики целиком.

    ⚠ Старое не вытесняется: таблица растёт без предела, обрезает только чтение
    своим `limit`.
    """
    sqllite3_llm_short_memory_init()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO messages (user_id, role, agent, content)
        VALUES (?, ?, ?, ?)
    ''', (user_id, role, agent, content))
    conn.commit()


def sqllite3_llm_short_memory_messages(user_id, agent, limit=50) -> list:
    """Последние реплики агента — от старых к новым, готовыми к подстановке в промпт.

    Args:
        user_id: с кем идёт разговор.
        agent: чью память читаем.
        limit: сколько последних реплик взять.

    Returns:
        list: тексты реплик — **только `content`**, без ролей и времени.

    ⚠ Отбираются последние `limit` записей, после чего порядок переворачивается:
    наружу идёт хронология, а не `ORDER BY id DESC` запроса.
    """
    sqllite3_llm_short_memory_init()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT content FROM messages 
        WHERE user_id = ? AND agent = ? 
        ORDER BY id DESC 
        LIMIT ?
    ''', (user_id, agent, limit))

    # Переворачиваем, чтобы история шла от старых к новым
    rows = cursor.fetchall()

    # Генератор списка: берем msg[0] из каждой строки
    history = [row[0] for row in reversed(rows)]

    return history
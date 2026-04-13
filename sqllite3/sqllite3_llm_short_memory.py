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
    sqllite3_llm_short_memory_init()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO messages (user_id, role, agent, content)
        VALUES (?, ?, ?, ?)
    ''', (user_id, role, agent, content))
    conn.commit()


def sqllite3_llm_short_memory_messages(user_id, agent, limit=50) -> list:
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
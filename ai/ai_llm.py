from mysql_.mysql_ import mysql_get_db_async

LLM_PROVIDERS = ('openrouter', 'claude', 'ollama', 'gemini', 'groq', 'mistral', 'huggingface', 'cerebras', 'openai')


def ai_llm_provider(model_name: str) -> str | None:
    for provider in LLM_PROVIDERS:
        if (model_name or '').lower().startswith(provider + '/'):
            return provider
    return None


async def ai_llm_node_get(node_id: int | None) -> list[dict]:
    """Список LLM ноды (с фоллбэком на catchall-ноду для агентов без своей ноды)."""
    effective_node_id = node_id or await ai_llm_node_id_default_get()

    async with mysql_get_db_async() as db:
        await db.execute(
            """
            SELECT l.* FROM `llm` l
            JOIN `node_llm` nl ON l.id = nl.llm_id
            WHERE nl.node_id = %s
            ORDER BY nl.rate_limit_at ASC, nl.position ASC
            """,
            (effective_node_id,)
        )
        return await db.fetchall()


async def ai_llm_node_id_default_get() -> int:
    """Нода-фоллбэк (node.is_catchall=1) для агентов без своей ноды (node_id IS NULL)."""
    async with mysql_get_db_async() as db:
        await db.execute("SELECT id FROM `node` WHERE is_catchall = 1 LIMIT 1")
        row = await db.fetchone()
        return row['id'] if row else None

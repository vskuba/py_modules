from qdrant_client import AsyncQdrantClient

from state.state import state_get

qdrant_client: AsyncQdrantClient | None = None


def _qdrant_db_get_client() -> AsyncQdrantClient | None:
    """
    Створює та повертає клієнт Qdrant (Singleton)
    """
    global qdrant_client

    if qdrant_client:
        return qdrant_client

    # Визначаємо хост залежно від режиму запуску (GUI/Docker)
    host = 'localhost' if state_get('mode_gui') else "qdrant"

    # Qdrant за замовчуванням використовує порт 6333 для HTTP API
    qdrant_client = AsyncQdrantClient(
        host=host,
        port=6333
    )

    return qdrant_client
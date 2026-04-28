from qdrant_client import AsyncQdrantClient

from state.state import state_get

qdrant_client: AsyncQdrantClient | None = None


def _qdrant_db_get_client() -> AsyncQdrantClient:
    """
    Створює та повертає клієнт Qdrant (Singleton)
    """
    global qdrant_client

    if qdrant_client:
        return qdrant_client

    # Qdrant за замовчуванням використовує порт 6333 для HTTP API
    qdrant_client = AsyncQdrantClient(
        host="qdrant",
        port=6333
    )

    if not qdrant_client:
        raise Exception("Failed to create Qdrant client")

    return qdrant_client

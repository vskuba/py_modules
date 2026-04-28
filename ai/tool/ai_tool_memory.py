import uuid
from datetime import datetime
from qdrant_client import AsyncQdrantClient
from sentence_transformers import SentenceTransformer
from qdrant_client.http.models import Distance, VectorParams, PointStruct, TextIndexParams, TokenizerType
from qdrant_client.http import models

from ai.tool.ai_tool_agent import agent_invoke
from logging_.logging_ import logger_info
from qdrant_.qdrant_ import _qdrant_db_get_client

encoder = SentenceTransformer("all-MiniLM-L6-v2", device='cpu')
qdrant_collection_name = 'bank_memory'
VECTOR_SIZE = 384


async def qdrant_memory_save(text: str, collection_name: str = ''):
    client: AsyncQdrantClient = _qdrant_db_get_client()
    col_name = collection_name if collection_name else qdrant_collection_name

    # 1. Создание коллекции с поддержкой текстового поиска
    if not await client.collection_exists(col_name):
        await client.create_collection(
            collection_name=col_name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        # Добавляем полнотекстовый индекс для поиска по словам (телефон, адрес и т.д.)
        await client.create_payload_index(
            collection_name=col_name,
            field_name="content",
            field_schema=TextIndexParams(
                type=models.TextIndexType.TEXT,
                tokenizer=TokenizerType.WORD,
                min_token_len=2,
                lowercase=True,
            )
        )

    facts = await _vector_extract_facts_from_text(text)
    if not facts: return "No important facts found to save."

    saved_count = 0
    for fact in facts:
        # Проверка дублей (используем наш новый гибридный поиск)
        existing = await _qdrant_memory_raw_search(fact, col_name, limit=1)
        if existing and existing[0].score > 0.90:  # Для гибридного поиска порог выше
            logger_info(f"⏭️ Факт уже известен, пропускаю: {fact}")
            continue

        fact_vector = encoder.encode(fact).tolist()
        await client.upsert(
            collection_name=col_name,
            points=[
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=fact_vector,
                    payload={
                        "content": fact,
                        "source": "extracted_fact",
                        "created_at": datetime.now().isoformat(),
                    }
                )
            ]
        )
        saved_count += 1
    return f"Saved {saved_count} facts."


async def qdrant_memory_search_text(query: str, collection_name: str = '') -> str:
    results = await _qdrant_memory_raw_search(query, collection_name)
    if results:
        return "\n".join([res.payload.get('content', '') for res in results])
    return "No relevant memories found."


async def _qdrant_memory_raw_search(query: str, collection_name: str = '', limit: int = 5):
    client: AsyncQdrantClient = _qdrant_db_get_client()
    col_name = collection_name if collection_name else qdrant_collection_name

    if not await client.collection_exists(col_name):
        return []

    # 1. Получаем инфо о коллекции
    col_info = await client.get_collection(col_name)

    # 2. ОПРЕДЕЛЯЕМ ИМЯ ВЕКТОРА ПРАВИЛЬНО
    vector_name = None
    vectors_config = col_info.config.params.vectors

    # Если векторов несколько (они в словаре/map)
    if hasattr(vectors_config, 'map') and vectors_config.map:
        vector_name = list(vectors_config.map.keys())[0]
    # Если вектор один, но он может быть именованным в некоторых версиях
    elif isinstance(vectors_config, dict) and vectors_config:
        vector_name = list(vectors_config.keys())[0]

    query_vector = encoder.encode(query).tolist()

    # Полнотекстовый фильтр для поиска по словам (телефон и т.д.)
    search_condition = models.FieldCondition(
        key="content",
        match=models.MatchText(text=query)
    )

    # 3. ВЫПОЛНЯЕМ ЗАПРОС
    response = await client.query_points(
        collection_name=col_name,
        prefetch=[
            models.Prefetch(
                query=models.NearestQuery(nearest=query_vector),
                using=vector_name,  # Теперь здесь будет "fast-all-MiniLM-L6-v2" или None
                limit=limit
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),  # type: ignore
        query_filter=models.Filter(should=[search_condition]),
        limit=limit
    )

    return response.points


async def _vector_extract_facts_from_text(text: str) -> list[str]:
    # ... (код экстракции фактов остается без изменений) ...
    prompt = f"Analyze and extract facts: {text}"
    try:
        response = await agent_invoke(agent_name="thinker", prompt=prompt)
        if not response: return []
        return [l.strip("- ").strip() for l in response.split('\n') if len(l) > 5]
    except Exception as e:
        logger_info(f"❌ Fact extraction error: {e}")
        return []

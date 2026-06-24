import os

from logging_.logging_ import logger_info

# Переводим Hugging Face в 100% Offline-режим
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import uuid
from typing import Any
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

# Импортируем генераторы fastembed напрямую
from fastembed import TextEmbedding, SparseTextEmbedding

# Константы моделей
EMBEDDING_MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
SPARSE_MODEL = 'Qdrant/bm25'

# Глобальные инстансы для синглтонов
qdrant_client: AsyncQdrantClient | None = None
dense_encoder: TextEmbedding | None = None
sparse_encoder: SparseTextEmbedding | None = None


def _qdrant_db_get_client() -> AsyncQdrantClient:
    """
    Создает и возвращает клиент Qdrant (Singleton).
    """
    global qdrant_client
    if qdrant_client:
        return qdrant_client

    qdrant_client = AsyncQdrantClient(host="qdrant", port=6333)
    if not qdrant_client:
        raise Exception("Failed to create Qdrant client")
    return qdrant_client


def _init_encoders():
    """
    Инициализирует локальные энкодеры fastembed ровно один раз.
    """
    global dense_encoder, sparse_encoder
    if dense_encoder is None:
        dense_encoder = TextEmbedding(model_name=EMBEDDING_MODEL)
    if sparse_encoder is None:
        sparse_encoder = SparseTextEmbedding(model_name=SPARSE_MODEL)


async def qdrant_save(collection_name: str, metadata: dict, data: list[dict]) -> int:
    """
    Динамическое сохранение данных в Qdrant на основе схемы метаданных со строгой валидацией.
    """
    client = _qdrant_db_get_client()

    # Сохраняем вызовы для совместимости высокоуровневого метода client.add
    client.set_model(EMBEDDING_MODEL)
    client.set_sparse_model(SPARSE_MODEL)

    docs, payloads = [], []

    for index, r in enumerate(data):
        payload = {}

        if 'content' not in r:
            raise KeyError(
                f"Ошибка валидации в коллекции '{collection_name}': "
                f"В объекте под индексом {index} отсутствует обязательное текстовое поле 'content'."
            )

        for field_name in metadata.keys():
            if field_name not in r:
                raise KeyError(
                    f"Ошибка синхронизации коллекции '{collection_name}': "
                    f"Поле '{field_name}' заявлено в метаданных коллекции, "
                    f"но отсутствует в объекте данных под индексом {index}."
                )

            value = r[field_name]

            if field_name == 'tags' and isinstance(value, str):
                payload[field_name] = [t.strip() for t in value.split(',') if t.strip()]
            else:
                payload[field_name] = value

        docs.append(r['content'])
        payloads.append(payload)

    await client.add(
        collection_name=collection_name,
        documents=docs,
        metadata=payloads,
        ids=[str(uuid.uuid4()) for _ in docs]
    )

    return len(data)


async def qdrant_search(
        query_text: str,
        collection_name: str,
        metadata: dict,
        metadata_filter: dict[str, Any],
        limit: int = 5
) -> list[models.ScoredPoint]:
    logger_info(
        f"Qdrant search: collection_name={collection_name}, query_text={query_text}, metadata_filter={metadata_filter}, limit={limit}")

    client = _qdrant_db_get_client()

    if not await client.collection_exists(collection_name):
        return []

    collection_metadata = metadata

    must_conditions = []
    should_conditions = []

    # 1. Динамически распределяем фильтры на основе метаданных
    for field_name, value in metadata_filter.items():
        if value is None or field_name not in collection_metadata:
            continue

        field_info = collection_metadata[field_name]
        table_origin = field_info.get('table_name', '')

        if isinstance(value, list):
            for item in value:
                condition = models.FieldCondition(key=field_name, match=models.MatchValue(value=item))
                if table_origin == 'fact' and (field_name.endswith('_id') or field_name == 'id'):
                    must_conditions.append(condition)
                else:
                    should_conditions.append(condition)
        else:
            condition = models.FieldCondition(key=field_name, match=models.MatchValue(value=value))
            if table_origin == 'fact' and (field_name.endswith('_id') or field_name == 'id'):
                must_conditions.append(condition)
            else:
                should_conditions.append(condition)

    query_filter = models.Filter(
        must=must_conditions if must_conditions else None,
        should=should_conditions if should_conditions else None
    )

    # 2. ИСПРАВЛЕНИЕ: Автоматически определяем точные имена векторов из схемы коллекции Qdrant
    col_info = await client.get_collection(collection_name)
    vectors_config = col_info.config.params.vectors
    sparse_config = col_info.config.params.sparse_vectors

    # Динамически ищем имя плотного вектора (в нем длинное техническое название модели)
    dense_vector_name = ""
    if hasattr(vectors_config, 'map') and vectors_config.map:
        dense_vector_name = list(vectors_config.map.keys())[0]
    elif isinstance(vectors_config, dict) and vectors_config:
        dense_vector_name = list(vectors_config.keys())[0]

    # Динамически ищем имя разреженного вектора
    sparse_vector_name = "fast-sparse-bm25"  # значение по умолчанию
    if sparse_config and isinstance(sparse_config, dict):
        sparse_vector_name = list(sparse_config.keys())[0]

    # 3. Локально генерируем эмбеддинги
    _init_encoders()

    dense_vector = next(dense_encoder.embed([query_text])).tolist()

    sparse_res = next(sparse_encoder.embed([query_text]))
    qdrant_sparse_vector = models.SparseVector(
        indices=sparse_res.indices.tolist(),
        values=sparse_res.values.tolist()
    )

    # 4. Выполняем векторный запрос с точными системными именами векторов
    response = await client.query_points(
        collection_name=collection_name,
        prefetch=[
            # Первый префетч: Поиск по плотным семантическим векторам
            models.Prefetch(
                query=dense_vector,
                using=dense_vector_name,  # Теперь здесь будет точное имя, например "fast-dense-..."
                limit=limit * 2,
                filter=query_filter
            ),
            # Второй префетч: Поиск по разреженным векторам (BM25)
            models.Prefetch(
                query=qdrant_sparse_vector,
                using=sparse_vector_name,  # Точное имя разреженного вектора
                limit=limit * 2,
                filter=query_filter
            )
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),  # type: ignore
        limit=limit
    )

    return response.points


async def qdrant_remove_by(collection_name: str, metadata_filter: dict[str, Any]) -> Any:
    """
    Динамическое удаление точек из Qdrant по заданным фильтрам метаданных.
    Все переданные фильтры объединяются через логическое 'И' (must).
    """
    client = _qdrant_db_get_client()

    if not await client.collection_exists(collection_name):
        return None

    # 1. Получаем актуальную карту метаданных для этой коллекции
    from src.ai.ai_fact_collection import ai_collection_metadata_get
    collection_metadata = await ai_collection_metadata_get(collection_name)

    must_conditions = []

    # 2. Строим жесткие фильтры для удаления
    for field_name, value in metadata_filter.items():
        if value is None:
            continue

        # Проверяем, существует ли поле в метаданных, во избежание ошибок Qdrant
        if field_name not in collection_metadata:
            continue

        # Обработка списков (удаление по совпадению элемента в массиве tags)
        if isinstance(value, list):
            for item in value:
                must_conditions.append(
                    models.FieldCondition(
                        key=field_name,
                        match=models.MatchValue(value=item)
                    )
                )
        # Обработка одиночных значений (girl_id, category и др.)
        else:
            must_conditions.append(
                models.FieldCondition(
                    key=field_name,
                    match=models.MatchValue(value=value)
                )
            )

    # Если фильтры не сформировались, прерываем выполнение,
    # чтобы случайно не очистить всю коллекцию целиком
    if not must_conditions:
        logger_info(f"Предотвращена полная очистка коллекции '{collection_name}': пустой фильтр удаления.")
        return None

    query_filter = models.Filter(must=must_conditions)

    # 3. Выполняем удаление
    logger_info(f"Qdrant remove: collection_name={collection_name}, filter={metadata_filter}")

    response = await client.delete(
        collection_name=collection_name,
        points_selector=models.FilterSelector(filter=query_filter)
    )

    return response
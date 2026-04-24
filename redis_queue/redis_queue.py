from rq import Queue
from typing import Dict

from redis_.redis_ import redis_conn_get

_redis_queues_cache = {}


def redis_queue_init(queues: list[str]):
    conn = redis_conn_get()

    # Кэшируем объекты очередей внутри процесса
    for name in queues:
        if name not in _redis_queues_cache:
            _redis_queues_cache[name] = Queue(name, connection=conn)

    return _redis_queues_cache


def redis_queue_get(name: str) -> Queue:
    """
    Возвращает объект очереди. Если очереди нет в кэше, инициализирует её.
    """
    if name not in _redis_queues_cache:
        # Если кто-то забыл вызвать queue_init, или это другой процесс
        conn = redis_conn_get()
        _redis_queues_cache[name] = Queue(name, connection=conn)

    return _redis_queues_cache[name]


def redis_queue_get_all() -> Dict[str, Queue]:
    return _redis_queues_cache

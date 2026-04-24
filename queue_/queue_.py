import os

from redis import Redis
from rq import Queue
from typing import Dict, Any

_conn = None
_queues_cache = {}


def redis_conn_get():
    global _conn
    if _conn is None:
        redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        _conn = Redis.from_url(redis_url)
    return _conn


def queue_init(queues: list[str]):
    conn = redis_conn_get()

    for name in queues:
        if name not in _queues_cache:
            _queues_cache[name] = Queue(name, connection=conn)

    return _queues_cache


def queue_get(name: str) -> Dict[str, Any] | None:
    if name not in _queues_cache:
        conn = redis_conn_get()
        _queues_cache[name] = Queue(name, connection=conn)

    return _queues_cache.get(name)

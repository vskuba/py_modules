import os

from redis import Redis
from rq import Queue
from typing import Dict, Any

from state.state import state_get, state_set


def queue_init(queues: list[str]):
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    conn = Redis.from_url(redis_url)
    queues_list = {}

    for i in queues:
        queues_list[i] = Queue(i, connection=conn)

    queue_set(queues_list)

    return queues_list


def queue_get() -> Dict[str, Any] | None:
    """
    Возращает очереди в ввиде json из глобальной перменной queue
    """
    queue_list = state_get('queue')
    if queue_list is None:
        print("Список очередей пустой.")

    return queue_list


def queue_set(queue_list: Dict[str, Any]):
    """
    Сохраняет очереди в state
    """
    state_set('queue', queue_list)

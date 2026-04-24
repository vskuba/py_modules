import queue

from rq import Queue
from typing import Dict

_queues_cache: Dict[str, Queue] = {}


def queue_init(queues: list[str]):
    for name in queues:
        if name not in _queues_cache:
            _queues_cache[name] = queue.Queue()

    return _queues_cache


def queue_get(name: str) -> queue.Queue:
    if name not in _queues_cache:
        _queues_cache[name] = queue.Queue()

    return _queues_cache[name]


def queue_get_all() -> dict[str, queue.Queue]:
    return _queues_cache

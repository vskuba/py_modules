import queue

from typing import Dict

_queues_cache: Dict[str, queue.Queue] = {}


def queue_init(queues: list[str]):
    """Завести очереди списком имён — на старте приложения, разом.

    Args:
        queues: имена очередей; существующие не пересоздаются.

    Returns:
        Весь реестр «имя → очередь», включая заведённые прежде.

    ⚠ Реестр лежит в модульной переменной — один на процесс, и очереди из него
    переживают вызов. Это очереди `queue.Queue` внутри процесса, а не брокер:
    соседний контейнер их не увидит (для этого — `redis_queue`).
    """
    for name in queues:
        if name not in _queues_cache:
            _queues_cache[name] = queue.Queue()

    return _queues_cache


def queue_get(name: str) -> queue.Queue:
    """Очередь по имени — заводится при первом спросе, `queue_init` не обязателен."""
    if name not in _queues_cache:
        _queues_cache[name] = queue.Queue()

    return _queues_cache[name]


def queue_get_all() -> dict[str, queue.Queue]:
    """Весь реестр очередей «имя → очередь» — для обхода и диагностики.

    ⚠ Возвращается **сам** словарь, а не копия: правка результата меняет реестр.
    """
    return _queues_cache

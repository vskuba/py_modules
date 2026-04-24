from typing import Dict, Any
from state.state import state_get, state_set


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

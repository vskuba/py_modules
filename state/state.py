from typing import Dict, Any

state: Dict[str, Any] = {}


def state_get(key: str, default: Any | None = None) -> Any | None:
    """
    Возращает значение по ключу сохраненное в обьекте state. Если ключа нет, то возращает None
    """
    return state.get(key, default)


def state_set(key: str, value):
    """
    Сохраняет значение по ключу в обьекте state
    """
    state[key] = value

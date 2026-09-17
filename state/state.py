"""Состояние процесса: словарь, который переживает вызовы, но не рестарт.

Не хранилище и не настройка: живёт в памяти одного процесса, снаружи не виден
и не гарантирован после перезапуска. Для персистентного состояния кругов —
`setting_/setting_state.py`; здесь только то, что законно живёт пока процесс.
"""
from typing import Dict, Any

state: Dict[str, Any] = {}


def state_get(key: str, default: Any | None = None) -> Any | None:
    """Значение по ключу из состояния процесса; ключа нет — default."""
    return state.get(key, default)


def state_set(key: str, value):
    """Сохраняет значение по ключу в объекте state."""
    state[key] = value

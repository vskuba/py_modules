import asyncio
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from logging_.logging_ import logger_info

EventHandler = Callable[['Event'], Awaitable[None]]

_subscribers: dict[str, list[EventHandler]] = defaultdict(list)


@dataclass
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


def event_subscribe(event_type: str, handler: EventHandler):
    """Подписать корутину-обработчик на тип события.

    Args:
        event_type: имя события — произвольная строка, реестра типов нет.
        handler: корутинная функция, принимает `Event`.

    ⚠ Подписки лежат в модульном словаре и живут до конца процесса: отписки не
    существует, а повторный вызов добавит тот же обработчик вторым — и событие
    придёт в него дважды.
    """
    _subscribers[event_type].append(handler)


def event_emit(event_type: str, payload: dict[str, Any] | None = None):
    """Разослать событие подписчикам, не дожидаясь, пока они отработают.

    Args:
        event_type: имя события; без подписчиков вызов — пустая операция.
        payload: полезная нагрузка, ложится в `Event.payload`.

    ⚠ Обработчики уходят в `asyncio.create_task`, поэтому нужен **работающий event
    loop** — из синхронного кода вне loop вызов упадёт. Исключение обработчика
    сюда не вернётся: оно только запишется в журнал.
    """
    event = Event(type=event_type, payload=payload or {})

    for handler in _subscribers.get(event_type, []):
        asyncio.create_task(_event_handler_run(handler, event))


async def _event_handler_run(handler: EventHandler, event: Event):
    try:
        await handler(event)
    except Exception as e:
        backtrace = traceback.format_exc()
        logger_info(
            f"❌ Ошибка в обработчике события '{event.type}': {e}. Полный стек вызовов:\n{backtrace}"
        )

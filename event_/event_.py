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
    _subscribers[event_type].append(handler)


def event_emit(event_type: str, payload: dict[str, Any] | None = None):
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

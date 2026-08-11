import re
from datetime import datetime, timedelta, timezone as py_timezone
from typing import Any

from setting_.setting_ import setting_get


async def datetime_now(
        user_id: int | None = None,
        node_id: int | None = None
) -> datetime:
    """
    Текущее время в часовом поясе из настройки `timezone` ('+3', 'UTC+3', '-2').

    Возвращает naive datetime — именно в таком виде значение уходит в DATETIME-колонки
    MySQL. Обе стороны сравнения (запись heartbeat и проверка «протух ли он») обязаны
    брать время отсюда: datetime.now() в контейнере и NOW() в MySQL дают время своих
    часовых поясов, и при их расхождении сессия либо вечно «живая», либо мгновенно
    «мёртвая».
    """
    offset = await setting_get('timezone', '+0', user_id, node_id)

    return datetime_offset_now(offset)


def datetime_utc_now() -> datetime:
    """
    Текущее время UTC как naive datetime.

    Для колонок, которые читает фронтенд через `new Date(str + 'Z')` — он сам
    переводит значение в пояс браузера. datetime.now() там не годится: он вернёт
    пояс контейнера, совпадающий с UTC только по совпадению настроек образа.
    """
    return datetime.now(py_timezone.utc).replace(tzinfo=None)


def datetime_offset_now(offset: Any | None) -> datetime:
    """Текущее время, сдвинутое на часовое смещение вида '+3' / 'UTC-2'; мусор = UTC."""
    match = re.search(r'([+-]?\d+)', str(offset or ''))
    hours = int(match.group(1)) if match else 0

    return datetime_utc_now() + timedelta(hours=hours)

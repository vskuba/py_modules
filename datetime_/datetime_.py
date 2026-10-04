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
    return datetime_utc_now() + timedelta(hours=datetime_offset_hours(offset))


def datetime_offset_hours(offset: Any | None) -> int:
    """Часы из смещения вида '+3' / 'UTC-2' / 3; мусор и пустота — ноль."""
    match = re.search(r'([+-]?\d+)', str(offset or ''))

    return int(match.group(1)) if match else 0


def datetime_day_minutes(offset: Any | None = None) -> int:
    """Минуты от начала **местных** суток: 0 в полночь, 1439 в 23:59.

    Args:
        offset: часовое смещение вида '+3' / 'UTC-2' / 3; пусто и мусор — UTC.

    Returns:
        Число от 0 до 1439 — на него сверяют смены расписаний.

    ⚠ **Способ счёта один на проект, и это не придирка.** Копии считали двумя
    путями — `tm_hour * 60 + tm_min` по `time.gmtime` и целыми секундами от эпохи,
    — и сходились, лишь пока смещение целое в часах. Стоило одной копии отстать,
    как одно занятие вставало по одним часам, а соседнее по другим; со стороны это
    выглядит как «расписание не работает», причём по-разному у разных сайтов.

    ⚠ Смещение берётся готовым числом или строкой, а не из настройки: когда меток
    много, настройку читают один раз и раздают — иначе на каждую метку уйдёт
    запрос к базе.
    """
    local = datetime_offset_now(offset)

    return local.hour * 60 + local.minute


def datetime_offset_apply(value: datetime | None, offset: Any | None) -> datetime | None:
    """
    Хранимое UTC-время в часовом поясе настройки — для показа человеку.

    Нужна там, где время не берут «сейчас», а читают из базы: `created_at` трасс,
    `started_at` прогонов, отметки журналов. MySQL в контейнере живёт по UTC, и
    без сдвига человек видит метку на несколько часов раньше, чем событие
    случилось, — а сверяет он её с часами на стене.

    `None` пропускаем как есть: у пустой колонки нет времени, которое можно
    сдвинуть, и подставлять вместо него «сейчас» было бы враньём.
    """
    if value is None:
        return None

    return value + timedelta(hours=datetime_offset_hours(offset))


async def datetime_local(value: datetime | None,
                         user_id: int | None = None,
                         node_id: int | None = None) -> datetime | None:
    """
    То же, что `datetime_offset_apply`, но смещение берётся из настройки само.

    Удобно для разовых показов; когда меток много, дешевле прочитать настройку
    один раз и звать `datetime_offset_apply` — иначе на каждую метку уйдёт запрос.
    """
    return datetime_offset_apply(value, await setting_get('timezone', '+0', user_id, node_id))

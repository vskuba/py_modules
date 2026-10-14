"""Подписчики каналов с открытым номером телефона: строки для базы номеров.

Здесь только сбор: вход — в `telegram_session`, куда сохранять — решает
вызывающий. Из миллиона подписчиков канала номер показывают единицы — те, кому
он открыт настройками приватности: молчаливое большинство за этой стеной, и
это не поломка, а Telegram.
"""
import argparse
import asyncio
import json

# Украинский мобильный: 12 цифр с +380, код оператора — 3-я и 4-я цифры.
TELEGRAM_MEMBERS_COUNTRY = '380'


async def telegram_members_phones(client, channels: list, limit: int = 0) -> dict:
    """Читать подписчиков каналов и вернуть строки тех, у кого номер телефона виден.

    Args:
        client: авторизованный TelegramClient (см. `telegram_session_start`).
        channels: каналы — `@имя` или ссылки `t.me/имя`, можно вперемешку.
        limit: первых N подписчиков на канал; 0 — сколько канал отдаст.

    Returns:
        dict: {'rows': [{'phone', 'first_name', 'last_name', 'middle_name',
        'operator_code', 'username', 'channel'}], 'failed': [{'channel', 'err'}]} —
        номера нормализованы к виду +380XXXXXXXXX; `middle_name` всегда пуст:
        отчества Telegram не знает; у неукраинских номеров `operator_code` пуст;
        дубликаты номера по каналам не повторяются — побеждает первая встреча;
        канал, который не разрешён или не найден, — в `failed`, а не отказ всего
        сбора; подписчик без видимого номера в `rows` не попадает вовсе.

    ⚠ Больше ~10 000 последних подписчиков канал не отдаёт никому — это потолок
    MTProto, а не ошибка сборки: «все подписчики» крупного канала недостижимы.
    """
    rows, failed, seen = [], [], set()
    for channel in channels:
        name = str(channel).strip().rstrip('/').split('/')[-1].removeprefix('@')
        try:
            entity = await client.get_entity(name)
            async for user in client.iter_participants(entity, limit or None):
                if getattr(user, 'self', False) or not getattr(user, 'phone', None):
                    continue
                phone = _telegram_members_phone(user.phone)
                if not phone or phone in seen:
                    continue
                seen.add(phone)
                rows.append({
                    'phone': phone,
                    'first_name': user.first_name or '',
                    'last_name': user.last_name or '',
                    'middle_name': '',
                    'operator_code': _telegram_members_operator(phone),
                    'username': getattr(user, 'username', None) or '',
                    'channel': name,
                })
        except Exception as exc:
            # сорванный канал не должен хоронить остальные: имена каналов правят
            # руками, и одна опечатка — строка в ведомости, а не трейсбек
            failed.append({'channel': str(channel), 'err': f'{type(exc).__name__}: {exc}'})
    return {'rows': rows, 'failed': failed}


def _telegram_members_phone(raw: str) -> str:
    """Номер подписчика к канону базы: без пробелов и черточек, с ведущим '+'.

    Пусто — если из строки не осталось ни одной цифры: там был не номер.
    """
    digits = ''.join(c for c in str(raw) if c.isdigit())
    return f'+{digits}' if digits else ''


def _telegram_members_operator(phone: str) -> str:
    """Код оператора украинского номера: 0XX из +3800XX…; у чужих стран — пусто."""
    digits = phone.removeprefix('+')
    if digits.startswith(TELEGRAM_MEMBERS_COUNTRY) and len(digits) == 12:
        return f'0{digits.removeprefix(TELEGRAM_MEMBERS_COUNTRY)[:2]}'
    return ''


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Подписчики каналов с открытым номером: строки для базы номеров.')
    p.add_argument('channel', nargs='+', help='каналы: @имя или ссылки t.me/имя')
    p.add_argument('--phone', required=True, help='номер аккаунта (сессия уже цела — не спросит)')
    p.add_argument('--api-id', required=True)
    p.add_argument('--api-hash', required=True)
    p.add_argument('--session', required=True, help='путь к файлу сессии без .session')
    p.add_argument('--code', default='', help='код входа, если сессия цела не оказалась')
    p.add_argument('--code-file', default='', help='файл, в который положат код')
    p.add_argument('--limit', type=int, default=0, help='первые N подписчиков на канал (0 — все)')
    ns = p.parse_args()

    async def _run():
        from telegram_.telegram_session import telegram_session_start
        client = await telegram_session_start(ns.phone, ns.api_id, ns.api_hash, ns.session,
                                             code=ns.code, code_file=ns.code_file)
        return await telegram_members_phones(client, ns.channel, ns.limit)

    print(json.dumps(asyncio.run(_run()), ensure_ascii=False, indent=1))

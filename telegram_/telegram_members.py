"""Подписчики каналов и чатов с открытым номером телефона: строки для базы номеров.

Здесь только сбор: вход — в `telegram_session`, куда сохранять — решает
вызывающий, а как номер выглядит канонно — в `phone_.phone_country`. Список
подписчиков Telegram открывает не всем и не везде: чужой канал молчит, пока
аккаунт в нём не админ, а чаты, куда аккаунт вписан, отдают список сами.
Номер при этом показывают единицы — те, кому он открыт настройками
приватности: молчаливое большинство за этой стеной, и это не поломка, а
Telegram.
"""
import argparse
import asyncio
import json

from phone_.phone_country import phone_parse


async def telegram_members_phones(client, channels: list, limit: int = 0) -> dict:
    """Читать подписчиков каналов и вернуть строки тех, у кого номер телефона виден.

    Args:
        client: авторизованный TelegramClient (см. `telegram_session_start`).
        channels: что читать — `@имя`, ссылки `t.me/имя` или entity из
            `client.get_dialogs()` (свои чаты аккаунта); можно вперемешку.
        limit: первых N подписчиков на канал; 0 — сколько канал отдаст.

    Returns:
        dict: {'rows': [{'phone', 'first_name', 'last_name', 'middle_name',
        'operator_code', 'username', 'channel'}], 'failed': [{'channel', 'err'}]} —
        номера нормализованы к виду +380XXXXXXXXX; `middle_name` всегда пуст:
        отчества Telegram не знает; у неукраинских номеров `operator_code` пуст;
        дубликаты номера по каналам не повторяются — побеждает первая встреча;
        канал, который не разрешён или не найден, — в `failed`, а не отказ всего
        сбора; подписчик без видимого номера в `rows` не попадает вовсе.

    ⚠ Список подписчиков Telegram открывает не всем и не везде: чужой канал
    молчит, пока аккаунт в нём не админ; чаты, куда аккаунт вписан, отдают
    список сами. И всё равно дальше ~10 000 подписчиков список не идёт —
    потолок MTProto: «все подписчики» крупного канала недостижимы.
    """
    rows, failed, seen = [], [], set()
    for channel in channels:
        # строку правят руками (там опечатка — не трейсбек), entity приходит из
        # get_dialogs уже готовым: имя чата берётся из него самого
        if isinstance(channel, str):
            name = channel.strip().rstrip('/').split('/')[-1].removeprefix('@')
        else:
            name = getattr(channel, 'title', '') or str(channel)
        try:
            entity = await client.get_entity(channel)
            async for user in client.iter_participants(entity, limit or None):
                if getattr(user, 'self', False) or not getattr(user, 'phone', None):
                    continue
                parsed = phone_parse(user.phone)
                if not parsed['phone'] or parsed['phone'] in seen:
                    continue
                seen.add(parsed['phone'])
                rows.append({
                    'phone': parsed['phone'],
                    'first_name': user.first_name or '',
                    'last_name': user.last_name or '',
                    'middle_name': '',
                    'operator_code': parsed['operator_code'],
                    'username': getattr(user, 'username', None) or '',
                    'channel': name,
                })
        except Exception as exc:
            # сорванный канал не должен хоронить остальные: имена каналов правят
            # руками, и одна опечатка — строка в ведомости, а не трейсбек
            failed.append({'channel': name, 'err': f'{type(exc).__name__}: {exc}'})
    return {'rows': rows, 'failed': failed}


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

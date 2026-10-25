"""Диалоги аккаунта: где он состоит и что от этого видно.

Чужой канал не отдаёт подписчиков, пока аккаунт в нём не админ, а свой чат —
отдаёт; собирать номера с площадок начинается с вопроса «что я вообще вижу».
Обход `get_dialogs()` с фильтром по виду — эти строки каждый сборщик писал
руками заново; здесь они один раз.
"""
import argparse
import asyncio

from json_.json_out import json_out


async def telegram_dialogs_list(client, kind: str = '') -> list:
    """Список диалогов, где аккаунт состоит; фильтр по виду, имена целые.

    Args:
        client: авторизованный TelegramClient (см. `telegram_session_start`).
        kind: через запятую что оставить: 'channel' (вещательный канал),
            'megagroup' (группа до 200 000), 'group' (малый чат), 'user'
            (личка); пусто — все.

    Returns:
        [{'name': заголовок или имя, 'username': с '@' или '', 'id': id
        оболочки, 'kind': один из видов выше, 'entity': сам объект Telethon —
        годится в `iter_participants`/`get_entity` без разрешения имени}]

    ⚠ Пусто при живом аккаунте — не поломка: чужие каналы молчат, пока аккаунт
    в них не админ (`ChatAdminRequiredError` у `iter_participants`), — список
    это то, что аккаунту **видно**, а не то, что он читает до конца.
    """
    want = {k.strip() for k in kind.split(',') if k.strip()}
    rows = []
    for dialog in await client.get_dialogs():
        entity = dialog.entity
        # вид — по классу Telethon, а не по случайным атрибутам: у User
        # username и phone как раз и бывают пусты
        if type(entity).__name__ == 'User':
            kind_of = 'user'
        elif getattr(entity, 'broadcast', False):
            kind_of = 'channel'
        elif getattr(entity, 'megagroup', False):
            kind_of = 'megagroup'
        else:
            kind_of = 'group'
        if want and kind_of not in want:
            continue
        rows.append({'name': getattr(entity, 'title', '') or getattr(entity, 'first_name', '')
                     or str(entity),
                     'username': getattr(entity, 'username', '') or '',
                     'id': getattr(entity, 'id', None), 'kind': kind_of,
                     'entity': entity})
    return rows


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Диалоги аккаунта с фильтром по виду: что видно собирателю.')
    p.add_argument('--kind', default='',
                   help='виды через запятую: channel,megagroup,group,user (пусто — все)')
    p.add_argument('--phone', required=True, help='номер аккаунта (сессия цела — не спросит)')
    p.add_argument('--api-id', required=True)
    p.add_argument('--api-hash', required=True)
    p.add_argument('--session', required=True, help='путь к файлу сессии без .session')
    p.add_argument('--code', default='', help='код входа, если сессия цела не оказалась')
    p.add_argument('--code-file', default='', help='файл, в который положат код')
    ns = p.parse_args()

    async def _run():
        from telegram_.telegram_session import telegram_session_start
        client = await telegram_session_start(ns.phone, ns.api_id, ns.api_hash, ns.session,
                                             code=ns.code, code_file=ns.code_file)
        return await telegram_dialogs_list(client, ns.kind)

    rows = asyncio.run(_run())
    # entity в печати не нужен: это объект, а не число — только шумело бы
    print(json_out([{k: v for k, v in row.items() if k != 'entity'} for row in rows]))

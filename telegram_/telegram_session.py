"""Вход в Telegram по MTProto: сессия авторизованного аккаунта — без неё подписчиков не видно.

Bot API списка подписчиков канала не отдаёт вовсе: «кто подписан и какой у него
номер» видно только из MTProto (Telethon), да и там номер виден, только если
подписчик своими настройками приватности открыл его другим. Здесь — вход: разовый
логин кодом из приложения Telegram и сессия, чтобы следующий вызов кода не просил.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

# Сколько секунд ждать, пока хозяин положит полученный код в файл. Логин — дело
# разовое, ожидание — не правило, а опция: код можно передать прямо в `code`,
# и тогда никто ни у кого ничего не ждёт.
TELEGRAM_SESSION_CODE_TIMEOUT = 600

_TELEGRAM_SESSION_CODE_POLL = 2


async def telegram_session_start(phone: str, api_id: int, api_hash: str,
                                session: str, code: str = '',
                                code_file: str = '', password: str = '',
                                code_timeout: float = TELEGRAM_SESSION_CODE_TIMEOUT):
    """Войти в Telegram под аккаунтом и вернуть уже авторизованный клиент Telethon.

    Сессия на диске есть и жива — никакой код не нужен вовсе; логин со своей
    сессией проходит молча. Код нужен на первом входе: `code` — готовый код
    прямо сейчас, или `code_file` — путь к файлу, куда хозяин положит код из
    приложения Telegram (ждём до `code_timeout` секунд).

    Args:
        phone: номер аккаунта вида +380… — на него приходит код.
        api_id и api_hash: ключи учётки с my.telegram.org.
        session: путь к файлу сессии без расширения — `.session` допишется сам.
        code: код из приложения; если задан, файл не ждём.
        code_file: путь к файлу, в который придёт код.
        password: пороль 2FA, если на аккаунте включена.
        code_timeout: сколько секунд ждать `code_file`.

    Returns:
        telethon.TelegramClient: авторизованный клиент — его ждут
        telegram_members_phones и любой другой MTProto-вызов.

    ⚠ Файл сессии — ключ к аккаунту: в репозиторий он не идёт, но утечка файла
    равна утечке аккаунта — читают его тем же способом, что и пишут.
    """
    from telethon import TelegramClient  # тяжёлая зависимость — лениво

    client = TelegramClient(str(Path(session).with_suffix('')), int(api_id), api_hash)
    await client.start(
        phone=phone,
        password=lambda: password,
        code_callback=lambda: _telegram_session_code(code, code_file, code_timeout),
    )
    return client


async def telegram_session_me(client) -> dict:
    """На кого оформлена сессия: id, имя, username и номер самого аккаунта.

    Returns:
        dict: {id, username, first_name, last_name, phone, bot} — о самом
        аккаунте сбора, а не о подписчиках; поля, которых у аккаунта нет, пусты.
    """
    me = await client.get_me()
    return {
        'id': me.id,
        'username': getattr(me, 'username', None) or '',
        'first_name': me.first_name or '',
        'last_name': me.last_name or '',
        'phone': getattr(me, 'phone', None) or '',
        'bot': bool(getattr(me, 'bot', False)),
    }


def _telegram_session_code(code: str, code_file: str, timeout: float) -> str:
    """Код готов — он и есть ответ; иначе ждём файл, куда хозяин его положит.

    Ждём синхронным сном: колбэк вызывают изнутри `start()`, когда запрос кода
    уже отправлен и весь процесс занят только ожиданием этого кода.
    """
    if code:
        return code
    if not code_file:
        raise RuntimeError('нет ни кода, ни code_file: войти нечем')
    path = Path(code_file)
    waited = 0.0
    while waited < timeout:
        if path.is_file():
            text = path.read_text(encoding='utf-8').strip()
            if text:
                return text
        time.sleep(_TELEGRAM_SESSION_CODE_POLL)
        waited += _TELEGRAM_SESSION_CODE_POLL
    raise TimeoutError(f'код так и не появился в {path} за {timeout:g} с')


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Вход в Telegram под аккаунтом: сессия для MTProto и сводка аккаунта.')
    p.add_argument('--phone', required=True, help='номер аккаунта, на который придёт код')
    p.add_argument('--api-id', required=True, help='api_id учётки с my.telegram.org')
    p.add_argument('--api-hash', required=True, help='api_hash учётки')
    p.add_argument('--session', required=True, help='путь к файлу сессии без .session')
    p.add_argument('--code', default='', help='код из приложения прямо сейчас')
    p.add_argument('--code-file', default='', help='файл, в который положат код позже')
    p.add_argument('--password', default='', help='пороль 2FA, если включена')
    ns = p.parse_args()
    cli = asyncio.run(telegram_session_start(
        ns.phone, ns.api_id, ns.api_hash, ns.session,
        code=ns.code, code_file=ns.code_file, password=ns.password))
    print(json.dumps(asyncio.run(telegram_session_me(cli)), ensure_ascii=False, indent=1))

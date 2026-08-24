"""Auth-примитивы, общие для всех проектов.

Здесь то, что одинаково в любом сервисе с токенами и паролями: как принять токен
из заголовков, как захешировать и сравнить пароль, как сгенерировать токен.
Ролевые модели (кто админ, кому что можно) здесь нет — это решает каждый проект.

Токен принимается двумя способами: `Authorization: Bearer <token>` (общепринят)
и `X-API-KEY: <token>` (привычен интеграциям). `Bearer` необязателен — голый
токен в `Authorization` тоже принимается, так его шлют из curl чаще, чем по
спецификации.

Пароли лежат хешем (`bcrypt`). Хеш-заглушка (`AUTH_DUMMY_HASH`) — для
несуществующего логина: сравнение с ней стоит столько же, сколько с настоящим,
и по времени ответа перебрать имена не выйдет.
"""

import secrets

import bcrypt
from fastapi import Request


# Заголовки, которыми принимается токен. Два, потому что `Bearer` общепринят, а
# `X-API-KEY` привычен интеграциям.
AUTH_TOKEN_HEADERS = ('authorization', 'x-api-key')
AUTH_TOKEN_PREFIX = 'bearer '

# Хеш-заглушка для несуществующего логина: сравнение с ней стоит столько же,
# сколько с настоящим, и по времени ответа перебрать имена не выйдет.
AUTH_DUMMY_HASH = bcrypt.hashpw(b'-', bcrypt.gensalt()).decode()


def auth_password_hash(password: str) -> str:
    """Хеш пароля для записи в базу. Открытый пароль не хранится нигде."""
    return bcrypt.hashpw(str(password).encode(), bcrypt.gensalt()).decode()


def auth_token_new() -> str:
    """Новый токен доступа. 32 байта случайности — перебирать нечего."""
    return secrets.token_urlsafe(32)


def auth_token_of(request: Request) -> str:
    """Токен из заголовков запроса. Нет ни одного — пустая строка.

    `Bearer` необязателен: голый токен в `Authorization` тоже принимается — так
    его шлют из curl чаще, чем по спецификации, и отвергать за это жестоко.
    """
    for name in AUTH_TOKEN_HEADERS:
        value = str(request.headers.get(name) or '').strip()
        if not value:
            continue
        if value.lower().startswith(AUTH_TOKEN_PREFIX):
            return value[len(AUTH_TOKEN_PREFIX):].strip()

        return value

    return ''


async def auth_token_user(token: str, db, query: str) -> dict | None:
    """Владелец токена по строке. Нет такого — None, решает вызывающий.

    `db` и `query` приходят извне: примитив сам соединение не открывает и не
    знает схемы проекта — в какую таблицу смотреть, что выбрать (id, username,
    role и их JOINы) решает вызывающий. От `db` ожидается только
    `execute`/`fetchone`, это и даёт `mysql_get_db_async`.
    """
    await db.execute(query, (str(token),))
    row = await db.fetchone()
    return row if row else None

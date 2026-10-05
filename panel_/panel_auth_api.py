"""Вход, выход и «кто я» — три точки, больше панели не нужно.

Самая маленькая часть скелета и самая неприятная, если разойдётся: вход у трёх
панелей был написан трижды, и третья копия успела растерять половину правил —
свой запрос вместо общей проверки пары, своё правило про первого пользователя и
мгновенный отказ на неизвестном логине.

## ⚠⚠ Роль кладётся в куку вместе с именем

`panel_auth_required` читает её оттуда, чтобы не ходить в базу на каждый запрос.
Кука подписана `SessionMiddleware`, подделать роль с той стороны нельзя. Плата
одна: смена роли доезжает после перелогина, но не позже.

## ⚠ Почему не сошлось — наружу не уходит

«Нет такого пользователя» и «неверный пароль» — разные ответы только для того,
кто перебирает логины. Проверку пары делает `panel_auth_user_check`, и она
сравнивает хеш **всегда**, даже когда логина в базе нет: иначе быстрый отказ
выдавал бы, какие логины существуют.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from logging_.logging_ import logger_info
from pydantic import BaseModel, Field

from panel_.panel_auth import PanelUser, panel_auth_required, panel_auth_user_check

# Под каким путём живут точки. ⚠ Имя `/auth` закреплено: на него смотрит проба
# живости сессии в обвязке сюиты и клиент панели для разовых проверок.
PANEL_AUTH_API_PREFIX = '/auth'

# Потолки полей входа. Длиннее — не отказ по сути, а защита от тела на мегабайт:
# хеш считается намеренно медленно, и длинный пароль стоит процессорного времени.
PANEL_AUTH_API_NAME_MAX = 50
PANEL_AUTH_API_PASSWORD_MAX = 200


class PanelAuthLogin(BaseModel):
    """Пара логин-пароль."""

    username: str = Field(..., min_length=1, max_length=PANEL_AUTH_API_NAME_MAX)
    password: str = Field(..., min_length=1, max_length=PANEL_AUTH_API_PASSWORD_MAX)


def panel_auth_api_router(prefix: str = PANEL_AUTH_API_PREFIX,
                          on_login=None) -> APIRouter:
    """Роутер входа: `app.include_router(panel_auth_api_router())`.

    Args:
        prefix: под каким путём жить.
        on_login: что сделать после удачного входа — `on_login(user)`. Нужно
            проекту, который пишет такое событие в свою ленту; скелет про ленты
            не знает.

    ⚠ Роутер отдаётся функцией, а не переменной модуля: будь он переменной, два
    приложения в одном процессе (а так живут тесты) получили бы один объект с
    уже навешенными роутами.
    """
    router = APIRouter(prefix=prefix, tags=['auth'])

    @router.post('/login')
    async def auth_login(data: PanelAuthLogin, request: Request):
        """Заводит сессию. Не сошлось — 401 без подробностей о том, что именно."""
        user = await panel_auth_user_check(data.username, data.password)
        if not user:
            logger_info(f'[auth] неудачный вход: {data.username}')
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail='неверный логин или пароль')

        request.session['user_id'] = user.user_id
        request.session['username'] = user.username
        request.session['role'] = user.role
        logger_info(f'[auth] вход: {user}')

        if on_login is not None:
            on_login(user)

        return _said(user)

    @router.post('/logout')
    async def auth_logout(request: Request):
        """Гасит сессию.

        ⚠ Повторный выход не ошибка: результат тот же — сессии нет. Отвечать
        отказом на второй щелчок значило бы пугать человека тем, чего он и
        добивался.
        """
        request.session.clear()

        return {'result': 'ok'}

    @router.get('/me')
    async def auth_me(user: PanelUser = Depends(panel_auth_required)):
        """Кто сейчас в сессии — страница показывает это в шапке.

        ⚠ Ею же обвязка сюиты и клиент разовых проверок узнают, жива ли кука:
        ручка дешёвая и не трогает предметных таблиц.
        """
        return _said(user)

    return router


# ── Приватное ────────────────────────────────────────────────────────────────

def _said(user: PanelUser) -> dict:
    """Что отвечаем про человека. ⚠ Пароля и токенов здесь нет и быть не может."""
    return {'result': {'id': user.user_id, 'username': user.username,
                       'role': user.role}}

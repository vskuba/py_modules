"""Точки управления людьми: учётки, роли, токены доступа.

Живут в разделе настроек и открыты только администратору — как seed и бэкапы.
⚠ Право проверяется зависимостью `panel_auth_admin_required` в объявлении
каждого роута, а не в теле: забытую проверку тогда находит чтение файла, а не
разбор происшествия.

Решения «можно ли это сделать» принимает домен (`panel_user.py`), здесь только
разбор запроса и перевод его отказов в коды HTTP:

* `ValueError` → 422: запрос понят, но так делать нельзя (имя занято, короткий
  пароль, правка роли себе);
* `LookupError` → 404: править нечего.

## ⚠ Роутер отдаётся функцией, а не переменной модуля

`panel_user_api_router()` заводит `APIRouter` на вызове. Будь он переменной
уровня модуля, два приложения в одном процессе (а так живут тесты) получили бы
один и тот же объект с уже навешенными роутами.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from panel_.panel_auth import PanelUser, panel_auth_admin_required
from panel_.panel_user import (panel_user_create, panel_user_delete,
                               panel_user_list, panel_user_token_add,
                               panel_user_token_delete, panel_user_update)

# Под каким путём живут точки. Выносится константой, потому что на него ссылается
# страница настроек: разойдись они — страница тихо показывает пустой список.
PANEL_USER_API_PREFIX = '/api/users'


def panel_user_api_router() -> APIRouter:
    """Роутер людей: подключается `app.include_router(panel_user_api_router())`."""
    router = APIRouter(prefix=PANEL_USER_API_PREFIX, tags=['users'])

    @router.get('')
    async def users_list(user: PanelUser = Depends(panel_auth_admin_required)):
        """Учётки с ролями и выданными токенами. Значений токенов здесь нет."""
        return {'result': {'users': await panel_user_list()}}

    @router.post('', status_code=201)
    async def users_create(payload: dict,
                           user: PanelUser = Depends(panel_auth_admin_required)):
        """Заводит учётку."""
        try:
            return {'result': await panel_user_create(
                str(payload.get('username') or ''), str(payload.get('password') or ''),
                str(payload.get('role') or ''))}
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=str(e))

    @router.put('/{user_id}')
    async def users_update(user_id: int, payload: dict,
                           user: PanelUser = Depends(panel_auth_admin_required)):
        """Меняет пароль и роль. Пустое поле — «не трогать»."""
        try:
            return {'result': await panel_user_update(
                user_id, str(payload.get('password') or ''),
                str(payload.get('role') or ''), actor_id=user.user_id)}
        except LookupError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=str(e))

    @router.delete('/{user_id}')
    async def users_delete(user_id: int,
                           user: PanelUser = Depends(panel_auth_admin_required)):
        """Удаляет учётку вместе с её токенами. Предметные данные остаются."""
        try:
            await panel_user_delete(user_id, actor_id=user.user_id)
        except LookupError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=str(e))

        return {'result': 'ok'}

    @router.post('/{user_id}/token')
    async def users_token_add(user_id: int, payload: dict,
                              user: PanelUser = Depends(panel_auth_admin_required)):
        """Выдаёт токен. **Значение приходит только в этом ответе** — больше нигде."""
        try:
            return {'result': await panel_user_token_add(
                user_id, str(payload.get('note') or ''))}
        except LookupError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    @router.delete('/token/{token_id}')
    async def users_token_delete(token_id: int,
                                 user: PanelUser = Depends(panel_auth_admin_required)):
        """Отзывает токен. Учётка остаётся."""
        await panel_user_token_delete(token_id)

        return {'result': 'ok'}

    return router

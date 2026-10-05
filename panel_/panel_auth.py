"""Вход в панель и проверка прав: сессия или токен, права через договор доступа.

Два способа представиться:

* **сессия** — кука панели, ею ходит человек со страницы;
* **токен** — `Authorization: Bearer <token>` или `X-API-KEY`, им ходят скрипты
  и проверки; сессии у них нет и быть не может.

Проверка живёт здесь одна на всех: точки зовут зависимость, а она — эту
проверку. Новый способ представиться появляется сразу во всех точках, а не в
тех, куда его не забыли добавить.

## ⚠⚠ Право, а не имя роли

Точка объявляет, **что нужно уметь**, а не **кем быть**:

```python
@router.delete('/{id}', dependencies=[Depends(panel_auth_need('workflow:delete'))])
```

Сравнение `role == 'admin'` отсекает любую другую полноправную роль — учётка QA
получала 403, имея на бумаге все права. Поэтому полнота роли определяется
**совпадением набора прав** с каталогом, а не именем.

Какие права есть и что входит в роль — знает проект и отвечает через
`PanelAccess` (`panel_access.py`). Скелет не знает ни одного права по имени.

## ⚠ Роль читается из куки, а не из базы

Зависимость висит на каждой точке, а страница опрашивает их пачками каждые
пятнадцать секунд — запрос к базе на каждый вызов заметен. Плата одна: смена
роли доезжает после перелогина, но не позже.

У токена роль всегда свежая: он читается из базы на каждый запрос, сессии у
скрипта нет и кэшировать нечего.

## ⚠ Где лежат люди — здесь не знают

Ни одного запроса в этом модуле нет: учётки отдаёт хранилище
(`panel_store.py`). Один проект держит роль строкой в `user.role`, другой —
ссылкой на таблицу `role`; скелету достаточно, что ему вернут `id`, `username`
и `role` **именем**.
"""

import bcrypt
from fastapi import Depends, HTTPException, Request, status

from auth_.auth_primitive import AUTH_DUMMY_HASH, auth_token_of
from logging_.logging_ import logger_info

from panel_.panel_access import panel_access
from panel_.panel_store import panel_store

# Роль по умолчанию, когда её неоткуда взять: ни в куке, ни в базе. Пустая
# строка, а не `admin` и не `operator`, — у неё нет прав ни по какому каталогу,
# то есть «непонятно кто» получает наименьшее.
PANEL_AUTH_ROLE_NONE = ''


class PanelUser:
    """Кто выполняет запрос. Ровно то, что нужно журналу, страницам и правам."""

    def __init__(self, user_id: int, username: str,
                 role: str = PANEL_AUTH_ROLE_NONE):
        self.user_id = int(user_id)
        self.username = str(username)
        self.role = str(role or PANEL_AUTH_ROLE_NONE)

    @property
    def permissions(self) -> frozenset:
        """Что этот человек умеет — по договору доступа установки."""
        return panel_access().permissions_of(self.role)

    @property
    def is_admin(self) -> bool:
        """Полноправен ли: `admin` и служебные роли вроде `qa`.

        ⚠⚠ Сверяется **набор прав**, а не имя роли, — см. ⚠⚠ в шапке. Имя
        свойства осталось прежним: его читают шаблоны страниц, и переименование
        ради точности стоило бы правки каждой из них.
        """
        return panel_access().is_full(self.role)

    def can(self, *permissions) -> bool:
        """Умеет ли **всё** перечисленное. Пустой список — умеет (нечего мочь)."""
        mine = self.permissions

        return all(one in mine for one in permissions)

    def __repr__(self) -> str:
        return f'#{self.user_id} {self.username} ({self.role or "без роли"})'


async def panel_auth_required(request: Request) -> PanelUser:
    """Зависимость точек: пускает своего, чужому отвечает 401.

    Прав не спрашивает — только «свой ли». Точке, которой нужно право, нужна
    `panel_auth_need(...)`.
    """
    user_id = request.session.get('user_id', 0)
    username = request.session.get('username', '')

    if user_id and username:
        return PanelUser(user_id, username,
                         request.session.get('role', PANEL_AUTH_ROLE_NONE))

    token = auth_token_of(request)
    if token:
        user = await panel_auth_token_user(token)
        if user:
            return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='нужен вход')


def panel_auth_need(*permissions):
    """Зависимость точки, которой нужны права. Отдаёт `PanelUser`.

    ```python
    @router.delete('/{id}')
    async def drop(id: int, user = Depends(panel_auth_need('workflow:delete'))):
    ```

    ⚠ Требуются **все** перечисленные права, а не любое из них: точка, которой
    хватает одного из двух, обычно на самом деле две точки.

    ⚠ Проверка стоит в объявлении роута, а не в теле: право видно при чтении
    файла, и точку, где его забыли, находит чтение, а не разбор происшествия.

    ⚠ 403, а не 404: скрывать существование страницы незачем — она есть, и
    человек видит её у коллеги. «Нельзя» понятнее, чем «нет такой».
    """
    async def check(user: PanelUser = Depends(panel_auth_required)) -> PanelUser:
        if not user.can(*permissions):
            missing = [one for one in permissions if one not in user.permissions]
            logger_info(f'[auth] отказано {user}: не хватает прав {missing}')
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail='недостаточно прав для этой операции')

        return user

    return check


async def panel_auth_admin_required(
        user: PanelUser = Depends(panel_auth_required)) -> PanelUser:
    """Зависимость точек, которые правят или выносят установку: seed, бэкапы.

    ⚠ Нужна там, где право назвать нечем: устройство установки это не предметная
    операция, и заводить под него `install:backup` значило бы называть правом
    то, что на деле «полноправный ли ты».

    ⚠⚠ Полнота определяется набором прав, а не именем роли: иначе служебная
    полноправная учётка получала бы 403, имея на бумаге всё.
    """
    if not user.is_admin:
        logger_info(f'[auth] отказано {user}: нужна полноправная роль')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='нужна роль администратора')

    return user


async def panel_auth_token_user(token: str) -> PanelUser | None:
    """Владелец токена. Нет такого — `None`, решает вызывающий."""
    try:
        row = await panel_store().by_token(str(token))
    except Exception as e:
        logger_info(f'[auth] токен не проверился: {e}')
        return None

    return _user_of(row)


async def panel_auth_user_get(user_id: int) -> PanelUser | None:
    """Человек по номеру — с ролью, какой бы запрос её ни доставал."""
    return _user_of(await panel_store().get(user_id))


async def panel_auth_user_check(username: str, password: str) -> PanelUser | None:
    """Проверяет пару логин-пароль. Не сошлось — `None`, без подробностей.

    ⚠ Почему именно не сошлось, наружу не уходит: «нет такого пользователя» и
    «неверный пароль» — разные ответы только для того, кто перебирает логины.

    ⚠⚠ Хеш сравнивается **всегда**, даже когда логина в базе нет: иначе быстрый
    отказ на несуществующем логине выдавал бы, какие логины существуют.
    """
    row = await panel_store().by_name(str(username).strip())

    stored = str(row['password_hash']) if row else AUTH_DUMMY_HASH
    try:
        ok = bcrypt.checkpw(str(password).encode(), stored.encode())
    except (ValueError, TypeError):
        ok = False

    if not row or not ok:
        return None

    return _user_of(row)


# ── Приватное ────────────────────────────────────────────────────────────────

def _user_of(row) -> PanelUser | None:
    """Строка базы в `PanelUser`. Пусто — `None`, решает вызывающий.

    ⚠ `role` берётся через `.get`: у проекта с ролями в таблице связь бывает
    пустой (`LEFT JOIN` без роли), и `NULL` обязан значить «без роли», а не
    ронять запрос.
    """
    if not row:
        return None

    return PanelUser(int(row['id']), str(row['username']),
                     str(row.get('role') or PANEL_AUTH_ROLE_NONE))

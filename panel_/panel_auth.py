"""Вход в панель и проверка «свой ли»: две роли, сессия или токен.

Ролей две, и это не матрица прав, а один вопрос: пускать ли к устройству
установки.

| Роль | Что доступно |
|------|--------------|
| `operator` | предметная работа — всё, ради чего панель открывают |
| `admin` | то же плюс seed, бэкапы, настройки и веб-морда к базе |

Дробить дальше не надо, пока не появился третий род работы. Здесь их два, и
граница между ними одна: обычная работа с данными против операций, которые
правят или выносят установку целиком.

## ⚠ Прав-возможностей здесь нет намеренно

`docs/auth_rules.md` описывает более крупную схему — роли в базе, права
строками. Пока ролей две и вопрос один, таблица прав была бы формой без
содержания. Признак пересмотреть: появилась роль, которой доступна **часть**
предметных страниц.

## Два способа представиться

* **сессия** — кука панели, ею ходит человек со страницы;
* **токен** — `Authorization: Bearer <token>` или `X-API-KEY`, им ходят скрипты
  и проверки; сессии у них нет и быть не может.

Пароли лежат хешем (`bcrypt`). Сравнение всегда идёт по хешу, даже когда логина
нет в базе: иначе время ответа выдало бы, существует ли такой пользователь.

## ⚠⚠ Схема таблиц — часть договора, а не деталь проекта

Модуль ходит в `user` и `user_access_token` напрямую. Это и есть смысл общего
скелета: панель у всех проектов одна, и таблицы людей у неё одни. Проект, у
которого они другие, этот скелет не берёт — чинить его параметром «имя таблицы»
значит заводить ветку в общей библиотеке ради того, чего ещё не случилось.
"""

import bcrypt
from fastapi import Depends, HTTPException, Request, status

from auth_.auth_primitive import AUTH_DUMMY_HASH, auth_token_of
from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_get_db_async

# Роли. `admin` — единственная, дающая доступ к устройству установки; всё
# остальное считается обычной работой. Сравнение идёт с этой константой, а не со
# строкой на месте: опечатка в строковом литерале открыла бы доступ молча.
PANEL_AUTH_ROLE_ADMIN = 'admin'
PANEL_AUTH_ROLE_OPERATOR = 'operator'

# Роли списком — для проверки на входе и для выпадающего списка на странице.
# ⚠ Список закрытый: опечатка вроде `admn` завела бы учётку, которая не может ни
# в предметные страницы, ни в настройки, а выглядела бы рабочей.
PANEL_AUTH_ROLES = (PANEL_AUTH_ROLE_ADMIN, PANEL_AUTH_ROLE_OPERATOR)


class PanelUser:
    """Кто выполняет запрос. Ровно то, что нужно журналу, страницам и правам."""

    def __init__(self, user_id: int, username: str,
                 role: str = PANEL_AUTH_ROLE_OPERATOR):
        self.user_id = int(user_id)
        self.username = str(username)
        # ⚠ Неизвестное значение роли считается наименьшими правами: строка в
        # базе может оказаться какой угодно, и трактовать «непонятно что» как
        # админа означало бы открывать доступ по опечатке.
        self.role = (PANEL_AUTH_ROLE_ADMIN if str(role) == PANEL_AUTH_ROLE_ADMIN
                     else PANEL_AUTH_ROLE_OPERATOR)

    @property
    def is_admin(self) -> bool:
        """Пускать ли к seed, бэкапам и прочему устройству установки."""
        return self.role == PANEL_AUTH_ROLE_ADMIN

    def __repr__(self) -> str:
        return f'#{self.user_id} {self.username} ({self.role})'


async def panel_auth_required(request: Request) -> PanelUser:
    """Зависимость точек: пускает своего, чужому отвечает 401.

    Одна на весь проект: новый способ представиться появляется сразу везде, а не
    в тех точках, куда его не забыли добавить.

    ⚠ Роль читается **из куки**, а не из базы: зависимость висит на каждой
    точке, а страница опрашивает их пачками каждые пятнадцать секунд — запрос к
    базе на каждый вызов заметен. Плата одна: смена роли доезжает после
    перелогина, но не позже — кука живёт столько, сколько задал проект.
    """
    user_id = request.session.get('user_id', 0)
    username = request.session.get('username', '')

    if user_id and username:
        return PanelUser(user_id, username,
                         request.session.get('role', PANEL_AUTH_ROLE_OPERATOR))

    token = auth_token_of(request)
    if token:
        user = await panel_auth_token_user(token)
        if user:
            return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='нужен вход')


async def panel_auth_admin_required(
        user: PanelUser = Depends(panel_auth_required)) -> PanelUser:
    """Зависимость точек, которые правят или выносят установку: seed, бэкапы.

    ⚠ Отдельная зависимость, а не проверка в теле: право видно в объявлении
    роута, и точку, где её забыли, находит чтение файла, а не разбор
    происшествия.

    ⚠ 403, а не 404: скрывать существование страницы незачем — она есть, и
    человек видит её у коллеги. «Нельзя» понятнее, чем «нет такой».
    """
    if not user.is_admin:
        logger_info(f'[auth] отказано {user}: нужна роль {PANEL_AUTH_ROLE_ADMIN}')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='нужна роль администратора')

    return user


async def panel_auth_token_user(token: str) -> PanelUser | None:
    """Владелец токена. Нет такого — `None`, решает вызывающий.

    ⚠ У токена роль всегда свежая: он читается из базы на каждый запрос, сессии
    у скрипта нет и кэшировать нечего.
    """
    try:
        async with mysql_get_db_async() as db:
            await db.execute(
                'SELECT u.id, u.username, u.role FROM user_access_token t '
                '  JOIN user u ON u.id = t.user_id WHERE t.token = %s', (str(token),))
            row = await db.fetchone()
    except Exception as e:
        logger_info(f'[auth] токен не проверился: {e}')
        return None

    return (PanelUser(int(row['id']), str(row['username']), str(row['role']))
            if row else None)


async def panel_auth_user_check(username: str, password: str) -> PanelUser | None:
    """Проверяет пару логин-пароль. Не сошлось — `None`, без подробностей.

    ⚠ Почему именно не сошлось, наружу не уходит: «нет такого пользователя» и
    «неверный пароль» — разные ответы только для того, кто перебирает логины.

    ⚠⚠ Хеш сравнивается **всегда**, даже когда логина в базе нет: иначе быстрый
    отказ на несуществующем логине выдавал бы, какие логины существуют.
    """
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT id, username, password_hash, role FROM user WHERE username = %s',
            (str(username).strip(),))
        row = await db.fetchone()

    stored = str(row['password_hash']) if row else AUTH_DUMMY_HASH
    try:
        ok = bcrypt.checkpw(str(password).encode(), stored.encode())
    except (ValueError, TypeError):
        ok = False

    if not row or not ok:
        return None

    return PanelUser(int(row['id']), str(row['username']), str(row['role']))

"""Договор хранения людей: где лежат учётки, роли и токены — дело проекта.

Скелет панели знает **правила** (кого нельзя удалить, какой пароль годится, что
роль себе не меняют) и знает **точки** (`/api/users`). Чего он не знает — как
устроено хранилище: один проект держит роль строкой в `user.role`, другой
ссылкой `user.role_id` на таблицу `role`, третий может держать её где угодно ещё.

Разница в раскладке таблиц — вопрос логики проекта. Интерфейс один, и он здесь.

## ⚠⚠ Почему не «подменяемые запросы»

Первым заходом хранилище подменялось строками SQL: вот запрос списка, вот запрос
по токену. Так можно описать **чтение**, но не запись: проекту с `role_id` нужно
не только выбрать имя роли через `JOIN`, но и перевести имя обратно в номер при
заведении учётки. Половина договора получилась бы выразимой, половина — нет, и
второй проект всё равно писал бы своё.

Договор на **методы** выражает обе половины и не держит проект за форму запроса.

## Умолчание покрывает простой случай

`PanelUserStoreColumn` — роль строкой в `user.role`. Проекту с такой схемой
писать не надо ничего: он и стоит по умолчанию.

## Что обязан отдавать каждый метод

Учётка — словарь с ключами `id`, `username`, `role`, `created_at`. Проверка
пароля дополнительно просит `password_hash`. ⚠ `role` — **имя** роли строкой, а
не номер: правами ведает договор доступа, а он знает роли по именам.
"""

from abc import ABC, abstractmethod

from mysql_.mysql_ import mysql_get_db_async


class PanelUserStore(ABC):
    """Где живут люди панели. Реализуется проектом, зовётся скелетом."""

    @abstractmethod
    async def list(self) -> list:
        """Все учётки с их токенами: `tokens` — список `{id, note, tail, created_at}`.

        ⚠ Значений токенов здесь быть не должно: открытая страница иначе
        показывала бы действующие ключи всякому, кто стоит за спиной.
        """

    @abstractmethod
    async def get(self, user_id: int) -> dict | None:
        """Одна учётка без токенов. Нет такой — `None`."""

    @abstractmethod
    async def by_name(self, username: str) -> dict | None:
        """Учётка по логину — **с `password_hash`**, для проверки входа."""

    @abstractmethod
    async def by_token(self, token: str) -> dict | None:
        """Учётка по токену доступа. Нет такого — `None`."""

    @abstractmethod
    async def create(self, username: str, password_hash: str, role: str) -> int:
        """Завести учётку. Отдаёт её номер."""

    @abstractmethod
    async def update(self, user_id: int, password_hash: str = '',
                     role: str = '') -> None:
        """Сменить пароль и роль. ⚠ Пустое значение — «не трогать»."""

    @abstractmethod
    async def delete(self, user_id: int) -> None:
        """Убрать учётку. Токены уходят с ней."""

    @abstractmethod
    async def count(self) -> int:
        """Сколько всего учёток. По нему посев узнаёт свежую установку."""

    @abstractmethod
    async def roles_used(self) -> list:
        """Имена ролей, которые сейчас у кого-то стоят.

        Нужно проверке «последний полноправный»: полнота — свойство набора прав,
        и запросом её не выяснить.
        """

    @abstractmethod
    async def token_add(self, user_id: int, token: str, note: str) -> int:
        """Выдать токен. Отдаёт его номер."""

    @abstractmethod
    async def token_delete(self, token_id: int) -> None:
        """Отозвать токен. Учётка остаётся."""


class PanelUserStoreColumn(PanelUserStore):
    """Умолчание: роль лежит строкой в `user.role`.

    Схема, которую ждёт эта реализация:

    ```sql
    user              id, username, password_hash, role, created_at
    user_access_token id, user_id, token, note, created_at  -- FK, ON DELETE CASCADE
    ```

    ⚠ `ON DELETE CASCADE` обязателен: выданный скрипту ключ не должен пережить
    человека, которому принадлежал.
    """

    # Сколько знаков токена показывать в списке. Хвоста хватает, чтобы отличить
    # свой ключ от чужого, и не хватает, чтобы им воспользоваться.
    TOKEN_TAIL = 6

    async def list(self) -> list:
        async with mysql_get_db_async() as db:
            await db.execute(
                'SELECT id, username, role, created_at FROM `user` ORDER BY id')
            rows = await db.fetchall() or []

            for row in rows:
                await db.execute(
                    'SELECT id, note, RIGHT(token, %s) AS tail, created_at'
                    '  FROM user_access_token WHERE user_id = %s ORDER BY id',
                    (self.TOKEN_TAIL, int(row['id'])))
                row['tokens'] = await db.fetchall() or []

        return rows

    async def get(self, user_id: int) -> dict | None:
        async with mysql_get_db_async() as db:
            await db.execute(
                'SELECT id, username, role, created_at FROM `user` WHERE id = %s',
                (int(user_id),))

            return await db.fetchone()

    async def by_name(self, username: str) -> dict | None:
        async with mysql_get_db_async() as db:
            await db.execute(
                'SELECT id, username, role, password_hash, created_at'
                '  FROM `user` WHERE username = %s', (str(username).strip(),))

            return await db.fetchone()

    async def by_token(self, token: str) -> dict | None:
        async with mysql_get_db_async() as db:
            await db.execute(
                'SELECT u.id, u.username, u.role, u.created_at'
                '  FROM user_access_token t JOIN `user` u ON u.id = t.user_id'
                ' WHERE t.token = %s', (str(token),))

            return await db.fetchone()

    async def create(self, username: str, password_hash: str, role: str) -> int:
        async with mysql_get_db_async() as db:
            await db.execute(
                'INSERT INTO `user` (username, password_hash, role)'
                ' VALUES (%s, %s, %s)', (username, password_hash, role))

            return int(db.lastrowid)

    async def update(self, user_id: int, password_hash: str = '',
                     role: str = '') -> None:
        fields, args = [], []
        if password_hash:
            fields.append('password_hash = %s')
            args.append(password_hash)
        if role:
            fields.append('role = %s')
            args.append(role)

        if not fields:
            return

        async with mysql_get_db_async() as db:
            await db.execute(f'UPDATE `user` SET {", ".join(fields)} WHERE id = %s',
                             tuple(args) + (int(user_id),))

    async def delete(self, user_id: int) -> None:
        async with mysql_get_db_async() as db:
            await db.execute('DELETE FROM `user` WHERE id = %s', (int(user_id),))

    async def count(self) -> int:
        async with mysql_get_db_async() as db:
            await db.execute('SELECT COUNT(*) AS n FROM `user`')

            return int(((await db.fetchone()) or {}).get('n') or 0)

    async def roles_used(self) -> list:
        async with mysql_get_db_async() as db:
            await db.execute('SELECT `role` FROM `user`')

            return [str(one.get('role') or '') for one in await db.fetchall() or []]

    async def token_add(self, user_id: int, token: str, note: str) -> int:
        async with mysql_get_db_async() as db:
            await db.execute(
                'INSERT INTO user_access_token (user_id, token, note)'
                ' VALUES (%s, %s, %s)', (int(user_id), token, note))

            return int(db.lastrowid)

    async def token_delete(self, token_id: int) -> None:
        async with mysql_get_db_async() as db:
            await db.execute('DELETE FROM user_access_token WHERE id = %s',
                             (int(token_id),))


def panel_store_use(store: PanelUserStore) -> None:
    """Подключить хранилище людей. Зовётся один раз, на старте.

    Raises:
        TypeError: подсунули не реализацию договора — иначе панель падала бы
            `AttributeError` на первом же входе, а выглядело бы это как поломка
            входа, а не как забытая настройка.
    """
    global _store

    if not isinstance(store, PanelUserStore):
        raise TypeError('хранилище обязано быть реализацией PanelUserStore, '
                        f'а не {type(store).__name__}')

    _store = store


def panel_store() -> PanelUserStore:
    """Хранилище людей установки. До подключения — роль строкой в `user.role`."""
    return _store


# ── Приватное ────────────────────────────────────────────────────────────────

# Хранилище установки. Одно на процесс: раскладка таблиц — свойство установки.
_store: PanelUserStore = PanelUserStoreColumn()

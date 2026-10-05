"""Люди панели: правила заведения, правки и посева первых двух.

Здесь только **правила**. Где лежат учётки — отвечает хранилище
(`panel_store.py`), какие бывают роли и права — договор доступа
(`panel_access.py`). Правила от раскладки таблиц не зависят и потому одни на все
панели.

## ⚠ Запреты ниже — не перестраховка

Каждый закрывает состояние, из которого нет выхода через панель:

* **нельзя менять роль себе** — понизив себя, администратор теряет доступ к этой
  самой странице, и вернуть роль ему будет некому. Роль меняет другой
  администратор — так это устроено везде, где роль вообще можно потерять;
* **нельзя удалить себя** — человек остаётся без учётки в середине сеанса;
* **нельзя тронуть последнего полноправного** — панель осталась бы без хозяина.

Последний запрет сегодня недостижим: до него не доходит очередь, потому что
единственный путь к нему — правка самого себя, а она отсечена раньше. Оставлен
намеренно, страховкой: правила «про себя» ещё будут меняться, а «панель без
администратора» — состояние, из которого выходят через базу.

## ⚠⚠ Посев: два условия, и они разные

* **администратор** (`ADMIN_USERNAME`) заводится, только когда таблица **пуста**.
  Это «первый пользователь»: пустая таблица и есть признак свежей установки, а
  на живой заводить второго админа по переменной окружения нельзя.
* **оператор** (`OPERATOR_USERNAME`) заводится, когда такого **логина** ещё нет.
  Вторая роль обычно появляется позже самой панели, и условие «таблица пуста»
  не дало бы завести его никогда.

Ни один из них не «создать или обновить»: правка пароля в `.env` молча меняла бы
его у живой учётки, а забытый в образце `change-me` возвращался бы после каждого
перезапуска.

Пароль здесь только принимается и хешируется. Открытым он не хранится, в журнал
не попадает даже частично: строка «сменил пароль #3» полезна, строка с паролем —
нет.
"""

import re

from auth_.auth_primitive import auth_password_hash, auth_token_new
from config.config import config_get
from logging_.logging_ import logger_info

from panel_.panel_access import panel_access
from panel_.panel_store import panel_store

# Что годится в имя учётки. Оно уходит в журналы и в списки, поэтому без пробелов
# и юникода: `оператор вася` в логе выглядит как поломка кодировки, а не как имя.
PANEL_USER_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]{2,50}$')

# Ниже этого пароль не принимаем. Восемь знаков — не «надёжно», а «не совсем
# смешно»: панель торчит в интернет, и словарный пароль подберут за вечер.
PANEL_USER_PASSWORD_MIN = 8

# Сколько знаков токена показывать в ответе на выдачу — столько же, сколько в
# списке. ⚠ Длину хвоста задаёт хранилище; здесь она нужна лишь затем, чтобы
# ответ на выдачу выглядел как строка списка.
PANEL_USER_TOKEN_TAIL = 6

# Пометка токена, который заводится вместе с первым пользователем: им ходят
# проверки и скрипты, и по пометке видно, что он служебный, а не чей-то личный.
PANEL_USER_SEED_TOKEN_NOTE = 'первый токен установки'


async def panel_user_list() -> list:
    """Учётки с ролями и выданными токенами — **без значений токенов**."""
    return await panel_store().list()


async def panel_user_get(user_id: int) -> dict | None:
    """Одна учётка без токенов. Нет такой — `None`, решает вызывающий."""
    return await panel_store().get(user_id)


async def panel_user_create(username: str, password: str, role: str) -> dict:
    """Заводит учётку. Пароль обязателен: без него человек не сможет войти.

    Raises:
        ValueError: имя не по шаблону, занято, короткий пароль, нет такой роли.
    """
    store = panel_store()

    username = str(username or '').strip()
    if not PANEL_USER_NAME_RE.match(username):
        raise ValueError('имя: латиница, цифры, точка, дефис, подчёркивание; 2-50 знаков')

    _password_check(password)
    role = _role_check(role)

    if await store.by_name(username):
        raise ValueError('такое имя уже занято')

    user_id = await store.create(username, auth_password_hash(password), role)
    logger_info(f'[auth][люди] заведена учётка «{username}» (#{user_id}), роль {role}')

    return await store.get(user_id)


async def panel_user_update(user_id: int, password: str = '', role: str = '',
                            actor_id: int = 0) -> dict:
    """Меняет пароль и роль. **Пустое поле — «не трогать»**.

    Не «поставить пустым»: иначе смена одной роли молча обнуляла бы пароль, и
    человек узнавал бы об этом при следующем входе.

    Args:
        actor_id: кто правит. По нему отсекается правка роли себе.

    Raises:
        LookupError: учётки нет.
        ValueError: короткий пароль, нет такой роли, правка роли себе,
            последний полноправный.
    """
    store = panel_store()

    current = await store.get(user_id)
    if not current:
        raise LookupError('учётка не найдена')

    if password:
        _password_check(password)

    if role and str(role) != str(current.get('role') or ''):
        role = _role_check(role)
        if int(user_id) == int(actor_id):
            raise ValueError('нельзя менять роль себе — это делает другой администратор')
        await _last_admin_check(current)
    else:
        role = ''

    # Ни одного поля — не ошибка: страница шлёт форму целиком, и «ничего не
    # изменил» это обычный исход, а не повод отвечать отказом.
    if not password and not role:
        return current

    await store.update(user_id, auth_password_hash(password) if password else '', role)

    logger_info(f'[auth][люди] учётка «{current["username"]}» (#{user_id}) изменена'
                + (', пароль' if password else '') + (f', роль → {role}' if role else ''))

    return await store.get(user_id)


async def panel_user_delete(user_id: int, actor_id: int) -> None:
    """Удаляет учётку вместе с её токенами.

    Токены уходят сами — по внешнему ключу с `ON DELETE CASCADE`. Так и надо:
    выданный скрипту ключ не должен пережить человека, которому принадлежал.

    ⚠ Предметные данные установки остаются: они принадлежат ей, а не человеку.

    Raises:
        LookupError: учётки нет.
        ValueError: удаление себя или последнего полноправного.
    """
    store = panel_store()

    target = await store.get(user_id)
    if not target:
        raise LookupError('учётка не найдена')

    if int(user_id) == int(actor_id):
        raise ValueError('нельзя удалить себя')

    await _last_admin_check(target)
    await store.delete(user_id)

    logger_info(f'[auth][люди] учётка «{target["username"]}» (#{user_id}) удалена')


async def panel_user_token_add(user_id: int, note: str = '') -> dict:
    """Выдаёт токен доступа. **Значение возвращается один раз — здесь.**

    Больше его взять негде: в списке отдаётся только хвост. Потерянный токен не
    восстанавливают, а отзывают и выдают новый.

    Raises:
        LookupError: учётки нет.
    """
    store = panel_store()

    if not await store.get(user_id):
        raise LookupError('учётка не найдена')

    token = auth_token_new()
    note = str(note or '').strip()[:190]
    token_id = await store.token_add(user_id, token, note)

    logger_info(f'[auth][люди] выдан токен #{token_id} учётке #{user_id}'
                + (f' — {note}' if note else ''))

    return {'id': token_id, 'token': token, 'note': note,
            'tail': token[-PANEL_USER_TOKEN_TAIL:]}


async def panel_user_token_delete(token_id: int) -> None:
    """Отзывает токен. Действует сразу: токен читается из базы на каждый запрос."""
    await panel_store().token_delete(token_id)

    logger_info(f'[auth][люди] отозван токен #{token_id}')


async def panel_user_seed_admin() -> None:
    """Заводит администратора, если учёток ещё **нет**. Молчит, если есть.

    Вместе с ним заводится токен: без него первую же проверку API пришлось бы
    делать куками из браузера, а это не тот способ, которым живут скрипты.
    """
    username = str(config_get('ADMIN_USERNAME', '')).strip()
    password = str(config_get('ADMIN_PASSWORD', ''))

    if not username or not password:
        logger_info('[auth] первый пользователь не заведён: '
                    'нет ADMIN_USERNAME/ADMIN_PASSWORD')
        return

    store = panel_store()
    if await store.count():
        return

    user_id = await store.create(username, auth_password_hash(password),
                                 panel_access().role_admin())
    await store.token_add(user_id, auth_token_new(), PANEL_USER_SEED_TOKEN_NOTE)

    logger_info(f'[auth] заведён первый пользователь «{username}» и токен доступа к нему')


async def panel_user_seed_operator() -> None:
    """Заводит оператора, если такого **логина** ещё нет.

    ⚠ Условие другое, чем у администратора, и это не оплошность — см. ⚠⚠ в
    заголовке модуля.

    Токена ему не полагается: оператор — человек за страницей, а не скрипт.
    Нужен токен — он заводится осознанно и с пометкой, кому выдан.
    """
    username = str(config_get('OPERATOR_USERNAME', '')).strip()
    password = str(config_get('OPERATOR_PASSWORD', ''))

    if not username or not password:
        logger_info('[auth] оператор не заведён: нет OPERATOR_USERNAME/OPERATOR_PASSWORD')
        return

    store = panel_store()
    if await store.by_name(username):
        return

    await store.create(username, auth_password_hash(password),
                       panel_access().role_operator())

    logger_info(f'[auth] заведён оператор «{username}»')


# ── Приватное ────────────────────────────────────────────────────────────────

def _password_check(password: str) -> None:
    if len(str(password or '')) < PANEL_USER_PASSWORD_MIN:
        raise ValueError(f'пароль короче {PANEL_USER_PASSWORD_MIN} знаков')


def _role_check(role: str) -> str:
    """Годится ли роль. Какие бывают — отвечает договор доступа установки.

    ⚠ `None` в ответе договора значит «роли заводятся на ходу»: закрытого списка
    нет, и проверять остаётся только непустоту.
    """
    role = str(role or '').strip()
    known = panel_access().roles_all()

    if not role or (known is not None and role not in known):
        raise ValueError(f'нет такой роли: {role or "пусто"}')

    return role


async def _last_admin_check(target: dict) -> None:
    """Не даёт тронуть последнего полноправного человека.

    Сегодня сюда не доходит очередь: единственный путь — правка самого себя, а
    она отсечена раньше. Проверка оставлена страховкой на случай, когда правила
    «про себя» изменятся: панель без администратора чинится только через базу.

    ⚠⚠ Считаются **полноправные**, а не роль с именем `admin`. Запросом это не
    сделать: полнота — свойство набора прав, и знает его договор установки, а не
    SQL. Поэтому роли перебираются в памяти; учёток у панели единицы, а зовётся
    проверка только на правке роли и удалении.
    """
    access = panel_access()
    if not access.is_full(str(target.get('role') or '')):
        return

    used = await panel_store().roles_used()
    full = sum(1 for one in used if access.is_full(one))

    if full <= 1:
        raise ValueError('это последний администратор — панель останется без хозяина')

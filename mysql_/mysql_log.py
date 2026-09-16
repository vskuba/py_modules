"""Строка запроса для журнала: та же, но без секретов в значениях.

## Зачем

Журнал печатает каждый запрос вместе со значениями (`| Args: (...)`), и это
правильно: без значений строка «UPDATE ... WHERE id = %s» не отвечает ни на один
вопрос отладки. Но через те же значения в журнал уезжает всё, чем машина
авторизуется:

* пароль учётки на чужом сайте — обычным аргументом `INSERT`;
* набор cookie после входа (`storage_state`) — целиком, килобайтами;
* пароли и ключи внутри JSON-колонки настроек;
* строка подключения к прокси вместе с паролем канала.

Журнал при этом уходит дальше самой машины: его читают в консоли, копируют в
переписку, отдают в поддержку и хранят месяцами. Секрет, попавший туда, считается
раскрытым — сменить его дешевле, чем доказывать обратное.

## Как решается

Два способа узнать секрет, и работают они вместе.

**По имени столбца.** Разбираем запрос и сопоставляем каждому `%s` имя столбца,
в который он едет: `SET password = %s` — третий аргумент, `INSERT INTO t (a, b)
VALUES (%s, %s)` — по списку столбцов. Имя из `MYSQL_LOG_SECRET_COLUMNS` —
значение заменяется меткой целиком.

**По содержимому.** Столбец `settings` секретным не назовёшь — в нём и адреса, и
сроки, — но `"api_password": "…"` внутри него секрет. Такие пары в JSON и пароль
в `scheme://user:pass@host` вырезаются из любого значения, куда бы оно ни ехало.

⚠ **Пустое значение остаётся пустым, а не превращается в метку.** «Пароль не
доехал» — самая частая беда в этих местах, и отличать её надо с одного взгляда.
Длину при этом не показываем: она сама по себе подсказка тому, кто подбирает.

⚠ **Разбор запроса не обязан быть полным.** Не узнали столбец — значение
печатается как есть, и это осознанно: журнал не должен молчать там, где разбор
споткнулся о незнакомую форму запроса. Защиту это не ослабляет — секреты по
содержимому ловятся всё равно.
"""

import re

# Чем заменяем секрет. Три точки, а не звёздочки: звёздочка встречается в самих
# значениях, и «***» в журнале читалось бы как часть данных.
MYSQL_LOG_MARK = '•••'

# Столбцы, значение которых секретно **всегда**, чем бы оно ни было. Список по
# смыслу, а не по проекту: имена одни и те же везде, где вообще есть учётки.
MYSQL_LOG_SECRET_COLUMNS = ('password', 'passwd', 'pass_hash', 'password_hash',
                            'secret', 'token', 'api_key', 'apikey', 'private_key',
                            'cookie', 'cookies', 'storage_state', 'proxy_url',
                            'credentials', 'auth')

# Ключи внутри JSON, значение которых секретно. Тот же список по смыслу; ищется
# вхождением, поэтому `companion_password` и `api_password` подходят оба.
MYSQL_LOG_SECRET_KEYS = ('password', 'passwd', 'secret', 'token', 'api_key',
                         'apikey', 'private_key', 'cookie')

# ⚠ Имя столбца берём **вплотную** перед `%s`: `SET password = %s`. Точка в имени
# (`g.password`) и обратные кавычки срезаются — сравнивается только имя.
_BEFORE = re.compile(r'[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s*(?:=|<=>|<>|!=|>=|<=|>|<|\bLIKE\b)\s*$',
                     re.IGNORECASE)

# Список столбцов у `INSERT`: по нему раскладываются `%s` внутри `VALUES`.
_INSERT = re.compile(r'INSERT\s+(?:IGNORE\s+)?INTO\s+[`"\w.]+\s*\(([^()]*)\)\s*VALUES',
                     re.IGNORECASE)

# Пара «ключ: значение» в JSON. Кавычки бывают экранированными (`\"`), когда JSON
# лежит строкой внутри JSON, — отсюда `\\?` в трёх местах.
_JSON_PAIR = re.compile(
    r'(\\?"[^"\\]*(?:' + '|'.join(MYSQL_LOG_SECRET_KEYS) + r')[^"\\]*\\?"\s*:\s*\\?")'
    r'([^"\\]+)(\\?")', re.IGNORECASE)

# Хвост вставки, чьи `%s` к списку столбцов не относятся.
_ON_DUPLICATE = re.compile(r'\bON\s+DUPLICATE\s+KEY\b', re.IGNORECASE)

# Пароль в строке подключения: `scheme://user:pass@host`.
_URL_AUTH = re.compile(r'(://[^\s/@:]+:)([^\s/@]+)(@)')


def mysql_log_line(query, args=None) -> str:
    """Готовая строка для журнала: запрос и значения, где секреты заменены меткой.

    Args:
        query: сам запрос, как он уйдёт в базу.
        args: кортеж значений либо `None`.

    Returns:
        str: `[MySQL SQL]: <запрос> | Args: (...)`; без значений — только запрос.

    ⚠ **Ничего не бросает.** Это журнал: он обязан напечатать строку при любом
    входе, включая тот, о котором разбор не думал. Споткнулись — печатаем как
    было, лишь бы запрос не потерялся.
    """
    if not args:
        return f'[MySQL SQL]: {query}'

    try:
        safe = mysql_log_args(query, args)
    except Exception:
        safe = args

    return f'[MySQL SQL]: {query} | Args: {safe}'


def mysql_log_args(query, args):
    """Значения запроса с вырезанными секретами. Форма — как у входа.

    Отдельно от `mysql_log_line`: значения нужны и тому, кто печатает их сам —
    в отчёте, в трассе, в тексте отказа.

    Кортеж и список разбираются по позициям; всё прочее (словарь именованных
    параметров, одиночное значение) чистится по содержимому — сопоставить их со
    столбцами нечем.
    """
    if isinstance(args, (list, tuple)):
        names = _names(str(query), len(args))
        out = [_value(one, names[i] if i < len(names) else '')
               for i, one in enumerate(args)]

        # Кортеж собираем обратно кортежем, а не `type(args)(...)`: у именованных
        # кортежей конструктор берёт поля по одному, и список туда не заходит.
        return tuple(out) if isinstance(args, tuple) else out

    if isinstance(args, dict):
        return {key: _value(one, str(key)) for key, one in args.items()}

    return _value(args, '')


def mysql_log_secret(name: str) -> bool:
    """Секретен ли столбец с таким именем. Пригодится и вне журнала.

    Сравнение вхождением: `api_password`, `password_hash` и `user_token`
    подходят все три — иначе список пришлось бы дописывать под каждый проект, а
    он общий.
    """
    low = str(name or '').rsplit('.', 1)[-1].strip('`"[] ').lower()

    return any(one in low for one in MYSQL_LOG_SECRET_COLUMNS)


# ── Приватное ────────────────────────────────────────────────────────────────

def _names(query: str, count: int) -> list[str]:
    """Имя столбца для каждого `%s` по порядку. Не разобрали — пустая строка.

    Две формы, и обе встречаются в каждом проекте:

    * `INSERT INTO t (a, b) VALUES (%s, %s)` — по списку столбцов. Список
      повторяется по кругу: многострочная вставка перечисляет `VALUES` пачками, а
      столбцы у них одни и те же;
    * всё остальное (`SET a = %s`, `WHERE b = %s`) — по тексту перед `%s`.
    """
    out, columns = [], []
    values_from, values_to = -1, -1

    found = _INSERT.search(query)
    if found:
        columns = [one.strip().strip('`"[] ') for one in found.group(1).split(',')]
        values_from = found.end()
        # ⚠ Хвост `ON DUPLICATE KEY UPDATE x = %s` из области `VALUES` исключаем:
        # его `%s` стоят после списка столбцов, но к нему отношения не имеют, и
        # раскладывать их по кругу значило бы назвать секретным что попало.
        dup = _ON_DUPLICATE.search(query, values_from)
        values_to = dup.start() if dup else len(query)

    seen = 0
    for spot in re.finditer(r'%s', query):
        if columns and values_from <= spot.start() < values_to:
            out.append(columns[seen % len(columns)])
            seen += 1
            continue

        before = _BEFORE.search(query[:spot.start()])
        out.append(before.group(1) if before else '')

    return out + [''] * max(0, count - len(out))


def _value(one, name: str):
    """Одно значение: секретное — меткой, прочее — с вычищенным содержимым."""
    if mysql_log_secret(name):
        # Пустое остаётся пустым: «не доехало» и «не показываем» — разные вещи.
        return one if one in (None, '', b'') else MYSQL_LOG_MARK

    if isinstance(one, str):
        return _clean(one)

    if isinstance(one, bytes):
        try:
            return _clean(one.decode('utf-8', 'replace')).encode('utf-8')
        except Exception:
            return one

    return one


def _clean(text: str) -> str:
    """Вырезает секреты из содержимого — общими правилами, а не своими.

    ⚠ **Правила живут в `logging_/logging_secret.py`**, потому что нужны не только
    журналу базы: тело ответа чужого сайта тоже уходит в журнал и тоже может
    нести пароль. Две копии этих регулярок разошлись бы в первый же день, когда
    список ключей пополнят, — и разошлись бы молча: журнал выглядел бы
    вычищенным.
    """
    from logging_.logging_secret import logging_secret_clean

    return logging_secret_clean(text)

"""
Текст SQL с длинными значениями: собрать так, чтобы ничего не слиплось.

Перенести строки между базами подстановкой `%s` нельзя: это не запрос к живому
соединению, а **файл**, который кто-то выполнит потом — миграция, разовый
скрипт, заготовка для прода. Значения приходится класть прямо в текст, и там их
подстерегает всё сразу: кавычки, обратные слэши, переносы строк, `NULL` против
пустой строки, чужое сличение.

## ⚠⚠ Зачем `FROM_BASE64`, а не экранирование

Экранирование надо делать безошибочно **каждый раз**; base64 — ни разу. В нём нет
ни кавычек, ни переносов, ни национальных букв, и он одинаково переживает
`mysql -e`, файл миграции и передачу через ssh.

Цена вопроса измерена. Самодельная сборка через `CONCAT_WS('\\x1e', …)` испортила
девять записей разом: MySQL принял `\\x1e` за **текст «x1e»**, а не за разделитель,
и все поля слиплись в первое. Восстанавливать пришлось из бэкапа.

## ⚠ Одно поле — одно присваивание

Соблазн собрать значения в строку и разобрать на месте велик, но разделителя,
который безопасен и в SQL, и в данных, не существует. Поэтому `sql_build_update`
делает по присваиванию на поле: команд больше, слипаться нечему.

## ⚠ `NULL` — это не пустая строка

`IFNULL(x, '')` теряет разницу между «значения нет» и «значение пусто», а она
бывает существенной: у `input`-переменной workflow пустая строка значит «ничего
не прислали», а `NULL` — «спросить умолчание». Поэтому `None` кладётся именно
`NULL`, а не пустым литералом.
"""
import argparse
import base64
import json
import re
import sys

# Имя объекта базы: буквы, цифры, подчёркивание. ⚠ Не «на всякий случай»:
# идентификатор в SQL подстановкой не передашь, и это единственная защита от
# чужого текста в запросе к боевой схеме.
SQL_BUILD_NAME_RE = re.compile(r'[A-Za-z0-9_]+')

# Длина значения, после которой кладём base64 вместо литерала. Короткие строки
# читаются глазами, и заворачивать их — прятать смысл от человека.
SQL_BUILD_INLINE_MAX = 60


def sql_build_value(value, collate: str = '') -> str:
    """
    Значение как кусок SQL: литерал, `NULL` или `FROM_BASE64(…)`.

    Args:
        value: что положить. `None` даёт `NULL`.
        collate: имя сличения — приписать `COLLATE`. ⚠ Нужно только там, где
            значение **сравнивается** со столбцом; для записи не нужно и вредно.

    Returns:
        Кусок SQL, готовый к подстановке в текст.

    ⚠ Короткие строки идут литералом с экранированием, длинные — base64: файл
    миграции читают глазами, и строка из ста шестидесяти знаков base64 вместо
    слова «привет» делает его нечитаемым без выигрыша.
    """
    if value is None:
        return 'NULL'

    if isinstance(value, bool):
        return '1' if value else '0'

    if isinstance(value, (int, float)):
        return str(value)

    text = str(value)
    tail = f' COLLATE {collate}' if collate and SQL_BUILD_NAME_RE.fullmatch(collate) \
        else ''

    if len(text) <= SQL_BUILD_INLINE_MAX and not _tricky(text):
        return "'" + text.replace('\\', '\\\\').replace("'", "''") + "'" + tail

    raw = base64.b64encode(text.encode('utf-8')).decode()

    return f"CONVERT(FROM_BASE64('{raw}') USING utf8mb4){tail}"


def sql_build_update(table: str, values: dict, where: dict,
                     collate: str = '') -> list:
    """
    Собрать UPDATE: по одному присваиванию на поле, длинный текст безопасно.

    Args:
        table: таблица.
        values: что записать; `None` кладётся как `NULL`.
        where: чем опознать строку.
        collate: сличение для сравнений в `WHERE` — когда базы расходятся.

    Returns:
        Список готовых команд, по одной на поле.

    Raises:
        ValueError: негодное имя таблицы или столбца.

    ⚠ **Список, а не одна команда.** Слить их в `SET a=…, b=…` можно, и соблазн
    велик, но тогда одна ошибка в длинном значении портит всю строку разом —
    ровно так и слиплись девять агентов. Раздельно ошибка стоит одного поля.
    """
    _name_check(table)
    if not where:
        raise ValueError('нужен `where`: UPDATE без условия правит всю таблицу')

    tail = ' AND '.join(
        f'`{_name_check(key)}` = {sql_build_value(val, collate)}'
        for key, val in where.items())

    return [f'UPDATE `{table}` SET `{_name_check(key)}` = {sql_build_value(val)}'
            f' WHERE {tail};'
            for key, val in values.items()]


def sql_build_insert(table: str, rows: list, ignore: bool = False) -> list:
    """
    `INSERT` по строке на запись.

    Args:
        table: таблица.
        rows: список словарей «столбец → значение».
        ignore: `INSERT IGNORE` — пропускать те, что уже есть.

    Returns:
        Список команд.

    ⚠ По команде на строку, а не одна с `VALUES (…), (…)`: у второй ошибка в
    одной записи отменяет все, а при переносе данных это худший исход — половина
    строк уехала, половина нет, и какая именно, неизвестно.
    """
    _name_check(table)
    word = 'INSERT IGNORE' if ignore else 'INSERT'
    out = []

    for row in rows or []:
        if not row:
            continue
        cols = ', '.join(f'`{_name_check(key)}`' for key in row)
        vals = ', '.join(sql_build_value(val) for val in row.values())
        out.append(f'{word} INTO `{table}` ({cols}) VALUES ({vals});')

    return out


def _name_check(name: str) -> str:
    """Имя таблицы или столбца — или отказ.

    ⚠ Идентификатор подстановкой не передаётся ни в одной базе, поэтому
    единственная защита — проверка на входе. Пропустить её здесь значит впустить
    чужой SQL в файл, который потом выполнят на проде.
    """
    text = str(name)
    if not SQL_BUILD_NAME_RE.fullmatch(text):
        raise ValueError(f'негодное имя: {text!r}')

    return text


def _tricky(text: str) -> bool:
    """Есть ли в строке то, что переживает экранирование плохо."""
    return any(ch in text for ch in ('\n', '\r', '\t', '\\', "'", '"', '\x00'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Собрать UPDATE или INSERT с длинными значениями безопасно.')
    parser.add_argument('command', choices=['update', 'insert'])
    parser.add_argument('table')
    parser.add_argument('--values', default='',
                        help='JSON «столбец: значение»; пусто — читаем stdin')
    parser.add_argument('--where', default='',
                        help='JSON «столбец: значение» для UPDATE')
    parser.add_argument('--collate', default='',
                        help='сличение для сравнений в WHERE')
    parser.add_argument('--ignore', action='store_true', help='INSERT IGNORE')
    args = parser.parse_args()

    try:
        payload = json.loads(args.values or sys.stdin.read() or '{}')

        if args.command == 'update':
            if not args.where:
                raise SystemExit('ошибка: для update нужен --where')
            lines = sql_build_update(args.table, payload,
                                     json.loads(args.where), args.collate)
        else:
            rows = payload if isinstance(payload, list) else [payload]
            lines = sql_build_insert(args.table, rows, args.ignore)

        print('\n'.join(lines))
    except (ValueError, json.JSONDecodeError) as err:
        raise SystemExit(f'ошибка: {err}')

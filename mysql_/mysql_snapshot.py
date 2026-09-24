"""
Одни и те же данные в двух базах: что разошлось, если отбросить автономера.

Сверять прод с рабочей базой приходится постоянно — промпты, настройки, реестры
правят и там, и там. Наивное сравнение не работает: `id` у баз свои, и строки,
одинаковые по смыслу, отличаются каждым числом. Поэтому сравнение идёт **по
ключу** (то, чем строка опознаётся: имя, позиция), а ссылки между строками
приводятся к тому же ключу.

Живая цена вопроса. Сверка двенадцати workflow дала «различаются все» — на деле
различался один номер шага внутри JSON. Ручной отпечаток через `GROUP_CONCAT`
соврал иначе: он молча режет результат на килобайте, и два разных набора шагов
дали одинаковый хеш.

## Что такое «ссылка»

Строка может указывать на другую строку той же таблицы: `step_id` в JSON
перехода, номер внутри имени переменной (`tool_45_result_str`). После переноса
между базами такая ссылка ведёт в пустоту, и это **самая тихая** из поломок:
переменной нет — секция промпта просто исчезает, а ответ выглядит обычным.

`--ref` описывает, где их искать. Без него сверка покажет ложное различие на
каждой ссылке. Замер на живой задаче: 23 «различия» без него и 2 настоящих с ним.

⚠ **Ссылок обычно несколько видов, и найти их можно только по остатку.** Порядок
работы такой: прогнать без `--ref`, посмотреть, чем отличаются строки, и на
каждый вид номера добавить свой. У workflow их вышло три:

    --ref 'metadata:_(\\d+)_result'          # номер шага в имени переменной
    --ref 'metadata:"step_id":\\s*"(\\d+)"'   # ссылка перехода
    --ref 'metadata:"workflow_id":\\s*(\\d+)' # вызов сабворкфлоу

## Чего он не делает

Не переносит. Показывает, что разошлось, — решение принимает человек; половина
расхождений оказывается правкой, которую как раз и не нужно затирать.
"""
import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

# Файл запускают путём (`python3 py_modules/mysql_/mysql_snapshot.py`). Тогда в
# путях первым лежит каталог файла, а в нём — `mysql_.py`; при поиске
# `import mysql_` обычный модуль побеждает пакет без `__init__.py`. Поэтому свой
# каталог из путей убираем, а `py_modules` ставим в начало.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Предел склейки у MySQL по умолчанию — 1024 знака, и `GROUP_CONCAT` режет молча.
# Отпечаток из обрезанной строки совпадает у разных наборов: так уже теряли
# различие в трёх шагах из двадцати четырёх.
MYSQL_SNAPSHOT_CONCAT_MAX = 100_000_000

# Сколько знаков значения показывать в отчёте. Дальше читать всё равно нечего:
# промпт на три килобайта глазами не сличают, для этого есть `--full`.
MYSQL_SNAPSHOT_VALUE_CUT = 70


def mysql_snapshot_take(table: str, key: str, where: str = '',
                        source: str = '') -> dict:
    """
    Снять срез таблицы: сравнить базы между собой, строки по ключу.

    Args:
        table: таблица.
        key: поля ключа через запятую — чем строка опознаётся в обеих базах
            (`name` или `workflow_id,position`). ⚠ Не `id`: автономера у баз
            свои, и по ним не совпадёт ни одна строка.
        where: условие отбора без слова `WHERE`.
        source: пусто — своя база; иначе команда, печатающая TSV
            (например, поход на прод по ssh).

    Returns:
        `{'rows': {ключ: {поле: значение}}, 'ids': {id: ключ}}`.
        `ids` нужен, чтобы приводить ссылки: в нём номер строки этой базы.

    Raises:
        RuntimeError: запрос не выполнился.
    """
    fields = [one.strip() for one in str(key).split(',') if one.strip()]
    if not fields:
        raise RuntimeError('нужен ключ: чем строка опознаётся в обеих базах')

    sql = (f"SET SESSION group_concat_max_len = {MYSQL_SNAPSHOT_CONCAT_MAX};"
           f" SELECT `id`, TO_BASE64(CONCAT_WS(CHAR(30),"
           + ', '.join(f'IFNULL(CAST(`{f}` AS CHAR), CHAR(0))' for f in fields)
           + f")), TO_BASE64(IFNULL(CAST(`{table}` AS JSON), '{{}}'))"
           f" FROM `{table}`" + (f' WHERE {where}' if where else ''))

    return _rows_read(sql, table, fields, where, source)


def mysql_snapshot_diff(left: dict, right: dict, refs: tuple = ()) -> list:
    """
    Чем срезы отличаются по смыслу, с приведением ссылок к ключам.

    Args:
        left: срез первой базы (обычно прод).
        right: срез второй.
        refs: поля-ссылки в виде `поле` или `поле:выражение`, где выражение —
            регулярка с одной группой-номером. Пример:
            `('metadata:"step_id":\\s*"(\\d+)"',)`.

    Returns:
        Список записей `{'key', 'field', 'left', 'right', 'kind'}`; `kind` —
        `only_left`, `only_right` или `changed`.

    ⚠ Ссылки приводятся **до** сравнения, а не после: иначе каждая из них даст
    ложное различие, и настоящее утонет среди них.
    """
    left_rows = _refs_resolve(left, refs)
    right_rows = _refs_resolve(right, refs)
    out = []

    for row_key in sorted(set(left_rows) | set(right_rows)):
        a, b = left_rows.get(row_key), right_rows.get(row_key)
        if a is None:
            out.append({'key': row_key, 'field': '', 'left': None,
                        'right': '', 'kind': 'only_right'})
            continue
        if b is None:
            out.append({'key': row_key, 'field': '', 'left': '',
                        'right': None, 'kind': 'only_left'})
            continue
        for field in sorted(set(a) | set(b)):
            if str(a.get(field)) != str(b.get(field)):
                out.append({'key': row_key, 'field': field,
                            'left': a.get(field), 'right': b.get(field),
                            'kind': 'changed'})

    return out


def mysql_snapshot_format(diff: list, left_name: str = 'левая',
                          right_name: str = 'правая', full: bool = False) -> str:
    """Отчёт о различиях словами. Пусто — совпадают."""
    if not diff:
        return 'совпадают полностью'

    lines, seen = [], None
    for item in diff:
        if item['key'] != seen:
            seen = item['key']
            lines.append(f"\n── {item['key']}")
        if item['kind'] == 'only_left':
            lines.append(f'   только в «{left_name}»')
            continue
        if item['kind'] == 'only_right':
            lines.append(f'   только в «{right_name}»')
            continue
        lines.append(f"   {item['field']}:")
        lines.append(f"     {left_name}:  {_cut(item['left'], full)}")
        lines.append(f"     {right_name}: {_cut(item['right'], full)}")

    changed = len({one['key'] for one in diff})
    lines.append(f'\nстрок с различиями: {changed}')

    return '\n'.join(lines).lstrip('\n')


def _rows_read(sql: str, table: str, fields: list, where: str,
               source: str) -> dict:
    """Выполнить запрос здесь или через чужую команду и разобрать TSV."""
    if source and source.startswith('@'):
        # Срез, снятый заранее: `--other @файл`. Безопасный путь и единственный,
        # если вторая база за семью дверями, — команду туда пишет человек, а сюда
        # приносит готовый TSV.
        lines = Path(source[1:]).read_text(encoding='utf-8').splitlines()
    elif source:
        # ⚠ `shell=True` здесь намеренно: `source` — команда, которую оператор
        # набрал бы сам (`ssh хост "docker exec … mysql -e '…'"`), с кавычками в
        # три слоя. Разобрать её списком аргументов нельзя, не отняв у инструмента
        # смысл. Чужого ввода тут нет: строка приходит из своей же командной
        # строки. Где это смущает — есть `--other @файл`.
        raw = subprocess.run(source, shell=True, capture_output=True,
                             text=True, timeout=300)
        if raw.returncode:
            raise RuntimeError(f'источник не ответил: {raw.stderr.strip()[:200]}')
        lines = raw.stdout.splitlines()
    else:
        from mysql_.mysql_query import mysql_query_run

        got = mysql_query_run(_select_plain(table, fields, where))
        lines = ['\t'.join([str(row['id']),
                            _b64(_join_key(row, fields)),
                            _b64(json.dumps(
                                {k: v for k, v in row.items() if k != 'id'},
                                ensure_ascii=False, sort_keys=True, default=str))])
                 for row in got['rows']]

    rows, ids = {}, {}
    for line in lines:
        part = line.rstrip('\n').split('\t')
        if len(part) != 3:
            continue
        row_id, key_b64, body_b64 = part
        key = _unb64(key_b64).replace('\x1e', ' / ')
        try:
            body = json.loads(_unb64(body_b64))
        except ValueError:
            body = {'_сырое': _unb64(body_b64)}
        rows[key] = body
        ids[int(row_id)] = key

    return {'rows': rows, 'ids': ids}


def _select_plain(table: str, fields: list, where: str) -> str:
    """Запрос для своей базы: разбирать будем на стороне Python."""
    return (f'SELECT * FROM `{table}`' + (f' WHERE {where}' if where else ''))


def _join_key(row: dict, fields: list) -> str:
    return '\x1e'.join(str(row.get(f, '')) for f in fields)


def _refs_resolve(snapshot: dict, refs: tuple) -> dict:
    """Заменить номера строк на их ключи — во всех полях-ссылках."""
    ids = snapshot.get('ids') or {}
    rows = {k: dict(v) for k, v in (snapshot.get('rows') or {}).items()}
    if not refs:
        return rows

    for spec in refs:
        field, _, pattern = str(spec).partition(':')
        pattern = pattern or r'_(\d+)_result'
        for body in rows.values():
            if field not in body or body[field] is None:
                continue
            body[field] = re.sub(
                pattern,
                lambda m: str(m.group(0)).replace(
                    m.group(1), f'«{ids.get(int(m.group(1)), "?")}»'),
                str(body[field]))

    return rows


def _b64(text: str) -> str:
    return base64.b64encode(str(text).encode('utf-8')).decode()


def _unb64(text: str) -> str:
    """Разбор base64, каким его печатает MySQL.

    ⚠ `TO_BASE64` **переносит строку каждые 76 знаков**, а в TSV перенос
    приезжает как литерал `\\n`. Оба вида пробельного мусора убираем здесь, а не
    в вызывающем: иначе каждый, кто снимает срез, споткнётся об это сам —
    сообщение об ошибке («485 не может быть на 1 больше кратного 4») о причине
    не говорит ничего.
    """
    raw = re.sub(r'\s|\\n', '', str(text))

    return base64.b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8', 'replace')


def _cut(value, full: bool) -> str:
    if value is None:
        return '—'
    text = ' '.join(str(value).split())

    return text if full or len(text) <= MYSQL_SNAPSHOT_VALUE_CUT \
        else text[:MYSQL_SNAPSHOT_VALUE_CUT] + f'… ({len(text)} зн)'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Сверка одной таблицы в двух базах по смыслу, а не по id.')
    parser.add_argument('table', help='таблица')
    parser.add_argument('--key', required=True,
                        help='чем строка опознаётся: `name` или `wf_id,position`')
    parser.add_argument('--where', default='', help='условие отбора без WHERE')
    parser.add_argument('--other', required=True,
                        help='команда, печатающая срез второй базы в TSV '
                             '(id, ключ base64, тело base64)')
    parser.add_argument('--ref', action='append', default=[],
                        help='поле-ссылка: `поле` или `поле:регулярка-с-группой`')
    parser.add_argument('--full', action='store_true', help='не обрезать значения')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    try:
        here = mysql_snapshot_take(args.table, args.key, args.where)
        there = mysql_snapshot_take(args.table, args.key, args.where, args.other)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

    got = mysql_snapshot_diff(there, here, tuple(args.ref))

    if args.json:
        print(json.dumps(got, ensure_ascii=False, indent=2, default=str))
    else:
        print(mysql_snapshot_format(got, 'та база', 'эта база', args.full))

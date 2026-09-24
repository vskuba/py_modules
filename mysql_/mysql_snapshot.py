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

## ⚠⚠ Сверка баз не ловит поломку, одинаковую в обеих

Это второй вопрос, и он **не сводится к первому**. Живой случай: у трёх шагов из
семи список внутри JSON лежал строкой вместо массива, движок падал на каждом
заходе — а сверка молчала, потому что прод и рабочая были испорчены **одинаково**.
Совпадение баз не означает, что обе верны; оно означает только, что они одинаковы.

Поэтому есть `mysql_snapshot_shape` — осмотр формы **внутри одной** базы, без
второй. Он спрашивает не «что разошлось», а «что здесь не как у соседей»:

    разнотипица  один путь JSON, а виды значений у строк разные
                 (7 строк ARRAY, 3 строки STRING — эти три и сломаны)
    проглочено   строка, которая сама разбирается как объект или массив
                 (контейнер, свёрнутый в текст: `"[{\\"a\\": 1}]"`)

⚠ Вторая примета сильнее первой и нужна отдельно: если поле есть **только** у
сломанных строк, большинства нет и сравнивать не с чем, а «строка, внутри которой
JSON» видна сама по себе.

## Чего он не делает

Не переносит. Показывает, что разошлось, — решение принимает человек; половина
расхождений оказывается правкой, которую как раз и не нужно затирать.

Не судит и о форме: «разнотипица» бывает законной (поле то пусто, то заполнено).
Инструмент называет место, человек решает.
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

# Насколько глубоко осматривать форму JSON. Два уровня — не компромисс, а мера:
# `$.llm_to_json_variable` лежит на первом, `$.workflow_invoke.outputs_map` — на
# втором, а глубже начинаются данные, где разнотипица законна.
MYSQL_SNAPSHOT_SHAPE_DEPTH = 2

# Сколько строк-примеров называть на каждый вид. Больше не нужно: по трём видно,
# что это за группа, а полный список — в базе.
MYSQL_SNAPSHOT_SHAPE_SHOW = 3


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
    return _rows_read(table, _key_fields(key), where, source)


def mysql_snapshot_sql(table: str, key: str, where: str = '',
                       columns: tuple = ()) -> str:
    """
    Запрос, которым снимает срез **вторая** база: её команду пишет человек.

    Args:
        table: таблица.
        key: поля ключа через запятую.
        where: условие отбора без слова `WHERE`.
        columns: имена столбцов; пусто — спросим у своей базы.

    Returns:
        Готовый SQL, печатающий три столбца TSV: `id`, ключ в base64, тело в
        base64 — ровно то, что ждёт `--other`.

    Raises:
        RuntimeError: столбцов не дали и своя база о таблице не знает.

    ⚠⚠ **Раньше этот запрос собирался, молча выбрасывался и вдобавок был
    неверен.** `_rows_read` получал его доводом и ни разу не использовал, а
    внутри стояло `CAST(таблица AS JSON)` — конструкция, которой в MySQL **нет**:
    `Unknown column 'agent_workflow' in 'field list'`. Мёртвый код при этом
    выглядел контрактом, и человек списывал с него команду для второй базы.
    Непрогоняемый код не проверяет никто — это его свойство, а не случайность.

    ⚠ Строка собирается `JSON_OBJECT` по именам столбцов: другого способа
    превратить строку таблицы в JSON у MySQL нет. Имена берутся у своей базы —
    схема у баз общая, иначе сверять было бы нечего.

    ⚠⚠ `id` в тело **не входит**, и это обязательно: автономера у баз свои, и
    попади он внутрь — различие показала бы каждая строка. Своя сторона его тоже
    выбрасывает (`_rows_read`), правило одно на обе.

    ⚠ `CHAR(30)` разделителем ключа, а не `'\\x1e'`: MySQL прочитал бы второе как
    текст «x1e» — та же ловушка, что испортила девять записей в `sql_build`.
    """
    fields = _key_fields(key)
    names = [str(one) for one in columns] or _columns_of(table)
    body = ', '.join(f"'{name}', `{name}`" for name in names if name != 'id')

    return (f'SET SESSION group_concat_max_len = {MYSQL_SNAPSHOT_CONCAT_MAX};'
            f' SELECT `id`, TO_BASE64(CONCAT_WS(CHAR(30), '
            + ', '.join(f'IFNULL(CAST(`{f}` AS CHAR), CHAR(0))' for f in fields)
            + f')), TO_BASE64(JSON_OBJECT({body}))'
            + f' FROM `{table}`' + (f' WHERE {where}' if where else ''))


def _columns_of(table: str) -> list:
    """Имена столбцов таблицы в своей базе."""
    from mysql_.mysql_query import mysql_query_run

    got = mysql_query_run(
        'SELECT `column_name` AS nm FROM `information_schema`.`columns`'
        ' WHERE `table_schema` = DATABASE() AND `table_name` = %s'
        ' ORDER BY `ordinal_position`', (str(table),))
    names = [str(row.get('nm')) for row in got['rows']]

    if not names:
        raise RuntimeError(
            f'своя база не знает таблицы `{table}` — передай столбцы доводом')

    return names


def _key_fields(key: str) -> list:
    """Поля ключа списком — или отказ, если ключа нет."""
    fields = [one.strip() for one in str(key).split(',') if one.strip()]

    if not fields:
        raise RuntimeError('нужен ключ: чем строка опознаётся в обеих базах')

    return fields


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


def mysql_snapshot_shape(snapshot: dict,
                         depth: int = MYSQL_SNAPSHOT_SHAPE_DEPTH) -> list:
    """
    Осмотр формы JSON внутри **одной** базы: что здесь не как у соседей.

    Args:
        snapshot: срез от `mysql_snapshot_take`.
        depth: до какого уровня вложенности смотреть.

    Returns:
        Список записей `{'kind', 'field', 'path', 'types', 'keys'}`, где `kind` —
        `conflict` (один путь, разные виды значений) или `swallowed` (строка, в
        которой лежит объект или массив).

    ⚠⚠ **Отсутствие пути — не разнотипица.** У шага-инструмента есть
    `$.tool_invoke`, у шага модели его нет вовсе, и это норма: сравниваются
    только те строки, где путь **есть**. Считай иначе — половина отчёта окажется
    шумом, и читать его перестанут.
    """
    kinds, swallowed = {}, {}

    for row_key, body in (snapshot.get('rows') or {}).items():
        for field, value in (body or {}).items():
            got = _json_container(value)
            if got is None:
                continue
            _shape_walk(got, field, '$', row_key, depth, kinds, swallowed)

    out = []

    for (field, path), seen in sorted(kinds.items()):
        if len(seen) < 2:
            continue
        out.append({'kind': 'conflict', 'field': field, 'path': path,
                    'types': {name: sorted(keys) for name, keys in seen.items()},
                    'keys': []})

    for (field, path), keys in sorted(swallowed.items()):
        out.append({'kind': 'swallowed', 'field': field, 'path': path,
                    'types': {}, 'keys': sorted(keys)})

    return out


def mysql_snapshot_shape_format(found: list, full: bool = False) -> str:
    """Отчёт об осмотре формы словами. Пусто — придраться не к чему."""
    if not found:
        return 'форма ровная: разнотипицы и проглоченных контейнеров нет'

    lines = []

    for item in found:
        where = f"{item['field']}{item['path'][1:]}" if item['path'] != '$' \
            else item['field']

        if item['kind'] == 'swallowed':
            keys = item['keys'] if full else item['keys'][:MYSQL_SNAPSHOT_SHAPE_SHOW]
            lines.append(f'\n── {where}: строка, а внутри JSON')
            lines.append(f"   строк: {len(item['keys'])}"
                         f" — {_keys_show(item['keys'], keys)}")
            continue

        lines.append(f'\n── {where}: виды значений не сходятся')
        for name, keys in sorted(item['types'].items(),
                                 key=lambda pair: (-len(pair[1]), pair[0])):
            show = keys if full else keys[:MYSQL_SNAPSHOT_SHAPE_SHOW]
            lines.append(f'   {name}: {len(keys)} — {_keys_show(keys, show)}')

    lines.append(f'\nмест с подозрением: {len(found)}')

    return '\n'.join(lines).lstrip('\n')


def _json_container(value):
    """Значение как объект или массив — или `None`, если это не контейнер.

    ⚠ Столбец JSON приезжает то разобранным, то строкой: из своей базы через
    драйвер он строка, из чужой команды — уже разобранный. Читаем оба вида, иначе
    осмотр работал бы только с одной стороны и молчал бы с другой.
    """
    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or text[0] not in '{[':
        return None

    try:
        got = json.loads(text)
    except ValueError:
        return None

    return got if isinstance(got, (dict, list)) else None


def _shape_walk(node, field: str, path: str, row_key: str, depth: int,
                kinds: dict, swallowed: dict) -> None:
    """Пройти уровень JSON, отмечая вид каждого пути и проглоченные контейнеры.

    ⚠ В массивы не спускаемся: путь с номером (`$.items[7]`) сравнивать не с чем
    — у соседней строки под тем же номером лежит другой элемент. Массив
    отмечается целиком, видом `ARRAY`.
    """
    if depth <= 0 or not isinstance(node, dict):
        return

    for name, value in node.items():
        here = f'{path}.{name}'
        kinds.setdefault((field, here), {}).setdefault(
            _json_kind(value), set()).add(row_key)

        # ⚠ Проглоченный контейнер ищем **внутри** разобранного JSON, а не у
        # столбца целиком: сам столбец — законная строка с JSON, и считать его
        # приметой значило бы поднимать тревогу на каждой строке таблицы.
        if isinstance(value, str) and _json_container(value) is not None:
            swallowed.setdefault((field, here), set()).add(row_key)

        if isinstance(value, dict):
            _shape_walk(value, field, here, row_key, depth - 1, kinds, swallowed)


def _json_kind(value) -> str:
    """Вид значения теми же словами, что говорит `JSON_TYPE` у MySQL.

    ⚠ Слова не свои намеренно: найдя в отчёте `STRING`, человек проверяет находку
    запросом `SELECT JSON_TYPE(…)` слово в слово, без перевода.

    ⚠ `bool` проверяется **до** `int`: в Python он его наследник, и порядок
    наоборот назвал бы `true` целым числом.
    """
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'BOOLEAN'
    if isinstance(value, dict):
        return 'OBJECT'
    if isinstance(value, list):
        return 'ARRAY'
    if isinstance(value, str):
        return 'STRING'
    if isinstance(value, int):
        return 'INTEGER'
    if isinstance(value, float):
        return 'DOUBLE'

    return type(value).__name__.upper()


def _keys_show(all_keys: list, show: list) -> str:
    """Перечисление ключей с хвостом «и ещё N»."""
    tail = len(all_keys) - len(show)

    return ', '.join(show) + (f' и ещё {tail}' if tail > 0 else '')


def _rows_read(table: str, fields: list, where: str, source: str) -> dict:
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
    parser.add_argument('--other', default='',
                        help='команда, печатающая срез второй базы в TSV '
                             '(id, ключ base64, тело base64); не нужна для '
                             '--sql и --shape')
    parser.add_argument('--ref', action='append', default=[],
                        help='поле-ссылка: `поле` или `поле:регулярка-с-группой`')
    parser.add_argument('--shape', action='store_true',
                        help='осмотр формы JSON внутри базы, без второй: '
                             'разнотипица и проглоченные контейнеры')
    parser.add_argument('--depth', type=int, default=MYSQL_SNAPSHOT_SHAPE_DEPTH,
                        help='глубина осмотра формы')
    parser.add_argument('--sql', action='store_true',
                        help='напечатать запрос для второй базы и выйти')
    parser.add_argument('--full', action='store_true', help='не обрезать значения')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    try:
        if args.sql:
            print(mysql_snapshot_sql(args.table, args.key, args.where))
            raise SystemExit(0)

        here = mysql_snapshot_take(args.table, args.key, args.where)

        # ⚠ Осмотр формы идёт **до** сверки и сам по себе: второй базы он не
        # требует, а поломка, одинаковая в обеих, видна только ему.
        if args.shape:
            found = mysql_snapshot_shape(here, args.depth)
            print(json.dumps(found, ensure_ascii=False, indent=2, default=str)
                  if args.json else mysql_snapshot_shape_format(found, args.full))
            raise SystemExit(0)

        if not args.other:
            raise SystemExit('ошибка: нужен --other (или --shape / --sql)')

        there = mysql_snapshot_take(args.table, args.key, args.where, args.other)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

    got = mysql_snapshot_diff(there, here, tuple(args.ref))

    if args.json:
        print(json.dumps(got, ensure_ascii=False, indent=2, default=str))
    else:
        print(mysql_snapshot_format(got, 'та база', 'эта база', args.full))

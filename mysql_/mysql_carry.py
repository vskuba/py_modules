"""
Перенести строки из одной базы в другую, переведя связи по естественному ключу.

Задача звучит просто — «скопируй `companion` с прода к себе», — а руками делается
долго и опасно: номера строк у баз свои, поэтому `INSERT` с чужим `id` либо
упирается в занятый номер, либо (хуже) приклеивает строку к **чужой** записи.
Связи надо переводить: номер у источника → естественный ключ → номер у получателя.

    PYTHONPATH=py_modules python -m mysql_.mysql_carry fact \\
      --source "ssh prod 'docker exec -i db mysql -N base'" \\
      --target "docker exec -i db mysql -N base" \\
      --key document_id --translate companion_id:companion:unique_id

## ⚠⚠ Номера строк не переносятся никогда

`id` у баз — автоинкремент, свой у каждой. Строка `companion` под номером 42 на
проде и локально — это два разных человека. Поэтому `id` из переноса исключается
всегда, а всё, что на него ссылалось, переводится через `--translate`.

⚠ Именно на этом ломаются переносы, сделанные «дампом одной таблицы»: связи
сохраняются численно и молча указывают не туда. Ошибка не всплывает до того дня,
когда кто-то откроет карточку и увидит чужие данные.

## ⚠⚠ Что не перевелось — не переносится, а называется

Строка, чей `companion_id` не нашёл пары у получателя, **не вставляется**, а
попадает в отчёт. Соблазн поставить `NULL` велик и дорог: получится запись без
хозяина, которую потом никто не опознает. Пусть лучше не будет строки, чем будет
строка-сирота.

## ⚠ Повтор ничего не удваивает

Вставка идёт `INSERT … SELECT … WHERE NOT EXISTS` по естественному ключу (`--key`),
и проверка живёт **внутри** вставки, а не отдельным запросом перед ней. Отдельный
запрос — это две операции, между которыми строка успевает появиться.

## ⚠⚠ Ключ обязан быть уникальным ПО СТРОКЕ, а не по смыслу

`--key` — то, чем строка узнаётся. Дай ему столбец, общий у нескольких строк
(`fact.document_id` — один на все факты человека), и поедет **только первая**:
остальные отсечёт та самая проверка, что спасает от повторов. Потеря будет тихой —
в отчёте напишется «поедет 30», а ляжет одна.

Поэтому ключ проверяется: нет на нём уникального индекса — инструмент говорит об
этом до сборки. У таблиц вроде `fact` переносить надо не по одному столбцу, а
отбором (`--where`) с последующей сверкой счёта.

## ⚠⚠ По умолчанию только показывает SQL

Записывать нужно сказать словом (`--apply`). Перенос идёт в **чужую** базу, и
увидеть текст до записи дешевле, чем разбирать последствия после. Бэкап получателя
до записи — обязателен: правило `deploy.md`, §4.5.
"""
import sys

from pathlib import Path

# Файл запускают и путём. Тогда первым в путях лежит каталог файла, и `import mysql_`
# находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mysql_.mysql_source import (mysql_source_ask, mysql_source_json,
                                 mysql_source_rows)
from mysql_.sql_export import sql_export_value

# Столбцы, которые не переносятся никогда. ⚠ `id` — см. ⚠⚠ в докстринге модуля.
MYSQL_CARRY_SKIP = ('id',)

# Сколько строк просить за раз. ⚠ Ответ приходит одним JSON, и на большой таблице
# он не влезет ни в память, ни в аргументы: отбор `--where` здесь не украшение.
MYSQL_CARRY_CHUNK = 500


def mysql_carry(table, source, target, key, translate=(), where='',
                chunk=MYSQL_CARRY_CHUNK) -> dict:
    """Перенести строки таблицы между базами: связи переводятся по ключу, id не едет.

    Args:
        table: таблица — одна и та же у источника и получателя.
        source: команда базы-источника (SQL на вход, ответ строками).
        target: команда базы-получателя.
        key: естественный ключ — столбец, по которому строка узнаётся в обеих
            базах (`unique_id`, `document_id`). Им же отсекается повтор.
        translate: переводы связей — `[(столбец, таблица, ключ), …]`: значение
            `столбца` это номер строки в `таблице`, узнаваемой по `ключу`.
        where: условие отбора строк у источника, без слова `WHERE`.
        chunk: сколько строк за один запрос.

    Returns:
        dict: `{sql, carried, lost, columns}` — текст для получателя, сколько строк
        поедет, список `{row, why}` непереносимых и перенесённые столбцы.

    Raises:
        RuntimeError: база не ответила; у таблицы нет `key`; `key` не уникален;
            перевод назван для столбца, которого в таблице нет.

    ⚠⚠ Ничего не записывает. Запись — `mysql_carry_apply`: собрать и применить
    разделено намеренно, чтобы текст можно было прочитать до записи в чужую базу.
    """
    columns = [one for one in _columns(source, table) if one not in MYSQL_CARRY_SKIP]
    if not columns:
        raise RuntimeError(f'{table}: столбцов не видно — база ответила пусто?')
    if key not in columns:
        raise RuntimeError(f'{table}: естественного ключа `{key}` нет среди столбцов')

    # ⚠⚠ Опечатка в переводе обязана падать, а не пропускаться. Молчаливый пропуск
    # — худший из возможных исходов: связи остаются числами источника и указывают у
    # получателя на **чужие** строки, а отчёт при этом говорит «перенесено».
    unknown = [one[0] for one in translate if one[0] not in columns]
    if unknown:
        raise RuntimeError(f"{table}: перевод назван для столбцов, которых нет: "
                           f"{', '.join(unknown)}")

    # ⚠ Уникальность ключа — см. ⚠⚠ в докстринге модуля: неуникальный отсечёт всё,
    # кроме первой строки, и сделает это молча.
    if not _unique(source, table, key):
        raise RuntimeError(f'{table}: на `{key}` нет уникального индекса — поедет '
                           f'только первая строка. Нужен ключ, узнающий строку.')

    maps = {one[0]: _translation(source, target, one[1], one[2])
            for one in translate}
    rows = _rows(source, table, columns, where, chunk)

    lines, lost = [], []
    for row in rows:
        ready, why = _translated(row, maps)
        if why:
            lost.append({'row': {key: row.get(key)}, 'why': why})
            continue

        lines.append(_insert(table, columns, ready, key))

    return {'sql': '\n'.join(lines), 'carried': len(lines), 'lost': lost,
            'columns': columns}


def mysql_carry_apply(plan, target) -> dict:
    """Применить собранный перенос к базе-получателю.

    Args:
        plan: то, что вернул `mysql_carry`.
        target: команда базы-получателя.

    Returns:
        dict: `{applied, said}` — прошло ли и что сказала база.

    ⚠ Вся вставка идёт **одной** подачей: строки независимы, а сотня отдельных
    запусков `docker exec` через ssh — это минуты вместо секунды.
    """
    if not plan.get('sql'):
        return {'applied': False, 'said': 'переносить нечего'}

    got = mysql_source_ask(target, plan['sql'])

    return {'applied': got['ok'], 'said': got['said'] or 'записано'}


def mysql_carry_format(plan) -> str:
    """Отчёт словами: сколько поедет, что не поедет и почему."""
    out = [f"Поедет строк: {plan['carried']}. "
           f"Столбцов: {len(plan['columns'])} (без `id`)."]

    if plan['lost']:
        out.append(f"\n⚠⚠ Не поедет — связь не перевелась: {len(plan['lost'])}")
        for one in plan['lost'][:10]:
            out.append(f"    {one['row']} — {one['why']}")
        if len(plan['lost']) > 10:
            out.append(f"    … и ещё {len(plan['lost']) - 10}")
        out.append('  Это не потеря: строка без хозяина хуже отсутствующей.')

    return '\n'.join(out)


def main() -> int:
    """CLI: по умолчанию печатает SQL; `--apply` записывает."""
    import argparse

    ap = argparse.ArgumentParser(
        description='Перенести строки между базами, переведя связи по ключу.')
    ap.add_argument('table', help='таблица')
    ap.add_argument('--source', required=True, help='команда базы-источника')
    ap.add_argument('--target', required=True, help='команда базы-получателя')
    ap.add_argument('--key', required=True, help='естественный ключ строки')
    ap.add_argument('--translate', action='append', default=[],
                    help='связь: столбец:таблица:ключ (можно несколько раз)')
    ap.add_argument('--where', default='', help='условие отбора у источника')
    ap.add_argument('--chunk', type=int, default=MYSQL_CARRY_CHUNK)
    ap.add_argument('--apply', action='store_true', help='записать в получателя')
    ns = ap.parse_args()

    links = []
    for one in ns.translate:
        parts = one.split(':')
        if len(parts) != 3:
            print(f'Связь пишется столбец:таблица:ключ, а не «{one}»')

            return 2
        links.append(tuple(parts))

    try:
        plan = mysql_carry(ns.table, ns.source, ns.target, ns.key,
                           translate=links, where=ns.where, chunk=ns.chunk)
    except RuntimeError as bad:
        print(f'Собрать перенос не вышло: {bad}')

        return 2

    print(mysql_carry_format(plan))

    if not ns.apply:
        print('\n── SQL (не записано, нужен --apply) ──')
        print(plan['sql'] or '(нечего)')

        return 0

    got = mysql_carry_apply(plan, ns.target)
    print(f"\n{'Записано' if got['applied'] else 'НЕ записано'}: {got['said']}")

    return 0 if got['applied'] else 1


def _columns(source, table: str) -> list:
    """Столбцы таблицы у источника, в порядке объявления."""
    rows = mysql_source_rows(
        source,
        'SELECT COLUMN_NAME FROM information_schema.COLUMNS '
        f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table}' "
        'ORDER BY ORDINAL_POSITION', what=f'{table}: столбцы')

    return [one[0].strip() for one in rows]


def _unique(source, table: str, key: str) -> bool:
    """Есть ли на столбце уникальный индекс — то есть узнаёт ли он строку.

    ⚠ Составной уникальный индекс, где `key` лишь первый столбец, уникальности
    столбцу не даёт — отсюда проверка на индекс **из одного** столбца.
    """
    rows = mysql_source_rows(
        source,
        'SELECT COUNT(*) FROM information_schema.STATISTICS s '
        'WHERE s.TABLE_SCHEMA = DATABASE() '
        f"AND s.TABLE_NAME = '{table}' AND s.NON_UNIQUE = 0 "
        f"AND s.COLUMN_NAME = '{key}' "
        'AND 1 = (SELECT COUNT(*) FROM information_schema.STATISTICS t '
        '          WHERE t.TABLE_SCHEMA = s.TABLE_SCHEMA '
        '            AND t.TABLE_NAME = s.TABLE_NAME '
        '            AND t.INDEX_NAME = s.INDEX_NAME)', what=f'{table}: индексы')
    head = rows[0][0].strip() if rows and rows[0] else '0'

    return head.isdigit() and int(head) > 0


def _rows(source, table: str, columns: list, where: str, chunk: int) -> list:
    """Строки источника словарями — через JSON, а не TSV.

    ⚠⚠ Именно JSON, а не TSV: в TSV `NULL` приходит словом `NULL` и неотличим от
    строки «NULL», а числа — от строк. Для переноса это разница между пустым полем и
    полем со словом внутри.

    ⚠ Обёртку в base64 и разбор делает `mysql_source_json` — там же объяснено, зачем
    она нужна и почему переводы строк снимаются в SQL. Здесь только запрос.
    """
    said = ', '.join(f"'{one}', `{one}`" for one in columns)

    return mysql_source_json(
        source,
        f'SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT({said})), JSON_ARRAY()) '
        f'FROM (SELECT * FROM `{table}`'
        + (f' WHERE {where}' if where else '')
        + f' LIMIT {int(chunk)}) one', what=f'{table}: строки')


def _translation(source, target, table: str, key: str) -> dict:
    """Перевод номеров: номер у источника → номер у получателя, через ключ.

    ⚠ Пара собирается из **двух** карт, а не запросом с двумя базами: базы в
    разных местах (одна за ssh), и соединить их одним SQL нельзя.
    """
    here, there = {}, {}

    for said, where, into in ((source, 'источник', here), (target, 'получатель', there)):
        for row in mysql_source_rows(said, f'SELECT `id`, `{key}` FROM `{table}`',
                                     what=f'{table}: {where} не отдал ключи'):
            if len(row) < 2:
                continue
            into[row[0].strip()] = row[1].strip()

    back = {natural: row_id for row_id, natural in there.items()}

    return {row_id: back[natural] for row_id, natural in here.items()
            if natural in back}


def _translated(row: dict, maps: dict) -> tuple:
    """Строка с переведёнными связями либо причина, почему перевести не вышло."""
    ready = dict(row)

    for column, table in maps.items():
        was = row.get(column)
        if was is None:
            continue

        now = table.get(str(was))
        if now is None:
            return {}, f'{column} = {was}: у получателя такой записи нет'

        ready[column] = int(now)

    return ready, ''


def _insert(table: str, columns: list, row: dict, key: str) -> str:
    """Одна вставка, не заводящая повтор: проверка внутри, а не перед.

    ⚠⚠ `WHERE NOT EXISTS` с обёрткой `(SELECT * FROM t) x`: MySQL не даёт читать
    ту же таблицу, в которую вставляет, напрямую — а без обёртки запрос падает
    `You can't specify target table`.
    """
    said = ', '.join(f'`{one}`' for one in columns)
    values = ', '.join(sql_export_value(row.get(one)) for one in columns)
    same = sql_export_value(row.get(key))

    return (f'INSERT INTO `{table}` ({said})\nSELECT {values}\n'
            f' WHERE NOT EXISTS (SELECT 1 FROM (SELECT * FROM `{table}`) x'
            f' WHERE x.`{key}` = {same});')


if __name__ == '__main__':
    sys.exit(main())

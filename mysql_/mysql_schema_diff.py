"""
Чем устройство одной базы отличается от другой: столбцы, типы, сличения, индексы, связи.

«Локально работает, на проде падает» — это почти всегда расхождение схемы, и
искать его глазами дорого: столбцов в этих базах под тысячу. Инструмент спрашивает
`information_schema` у обеих и показывает разницу.

    PYTHONPATH=py_modules python -m mysql_.mysql_schema_diff \\
      --left  "docker exec -i db mysql -N base" \\
      --right "ssh prod 'docker exec -i db mysql -N base'"

## ⚠⚠ Сличения — не мелочь оформления, а причина падений

Сегодняшняя цена вопроса: прод `utf8mb4_0900_ai_ci`, локально
`utf8mb4_general_ci`. Миграция, дважды прошедшая локально, на копии прода упала
`Illegal mix of collations` — и упала бы на живом проде. Причина тонкая:
**строковые литералы** приводятся к сличению столбца, а **пользовательские
переменные** — нет, их сличение «неявное». Поэтому расхождение видно только там,
где сравнивают переменную со столбцом.

Отсюда сличения здесь — находка первого разряда, наравне с отсутствующим
столбцом, а не примечание.

## Что сравнивается

| Вид | Почему это ломает |
|-----|-------------------|
| `нет столбца` | запрос падает `Unknown column` |
| `тип` | `int` против `bigint` — тихое усечение на больших числах |
| `сличение` | `Illegal mix of collations` там, где сравнивают с переменной |
| `null` | `NOT NULL` без умолчания на проде — вставка падает |
| `умолчание` | `NOT NULL DEFAULT CURRENT_TIMESTAMP` раздал всем строкам время `ALTER` |
| `нет индекса` | запрос работает, но полным проходом — находится под нагрузкой |
| `связь` | `ON DELETE` разный — строки-сироты либо каскад, которого не ждали |

⚠ `нет таблицы` не выносится в отдельный вид: таблицы нет — не будет и её
столбцов, и они скажут об этом сами. Отдельная строка удвоила бы отчёт.

## ⚠ Сторона «слева» — своя, «справа» — та, куда выкладывают

Порядок значим только для чтения отчёта: «слева есть, справа нет» читается как
«уедет и сломается». Поменяй стороны — тот же список прочтётся наоборот.
"""
import subprocess
import sys

from pathlib import Path

# Файл запускают и путём. Тогда первым в путях лежит каталог файла, и `import mysql_`
# находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Что снимается с базы. ⚠⚠ Один запрос на вид, а не один на всё: `UNION` свёл бы
# столбцы с индексами в одну плоскую таблицу, и разбор стал бы угадыванием.
#
# ⚠ `TABLE_SCHEMA = DATABASE()` — база берётся из самой команды `source`, а не
# доводом: команда уже содержит имя базы, и второе его написание разошлось бы.
MYSQL_SCHEMA_DIFF_SQL = {
    'columns': """
        SELECT CONCAT(TABLE_NAME, '.', COLUMN_NAME), COLUMN_TYPE,
               COALESCE(COLLATION_NAME, ''), IS_NULLABLE,
               COALESCE(COLUMN_DEFAULT, '—'), EXTRA
          FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE()
         ORDER BY TABLE_NAME, COLUMN_NAME""",
    'indexes': """
        SELECT CONCAT(TABLE_NAME, '.', INDEX_NAME),
               GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX),
               MAX(NON_UNIQUE)
          FROM information_schema.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
         GROUP BY TABLE_NAME, INDEX_NAME
         ORDER BY TABLE_NAME, INDEX_NAME""",
    'keys': """
        SELECT CONCAT(k.TABLE_NAME, '.', k.CONSTRAINT_NAME),
               CONCAT(k.COLUMN_NAME, ' → ', k.REFERENCED_TABLE_NAME, '.',
                      k.REFERENCED_COLUMN_NAME),
               CONCAT(r.DELETE_RULE, '/', r.UPDATE_RULE)
          FROM information_schema.KEY_COLUMN_USAGE k
          JOIN information_schema.REFERENTIAL_CONSTRAINTS r
            ON r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
           AND r.CONSTRAINT_SCHEMA = k.TABLE_SCHEMA
         WHERE k.TABLE_SCHEMA = DATABASE()
           AND k.REFERENCED_TABLE_NAME IS NOT NULL
         ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME""",
}

# Поля снимка по видам — ими же названы находки.
MYSQL_SCHEMA_DIFF_FIELDS = {
    'columns': ('тип', 'сличение', 'null', 'умолчание', 'extra'),
    'indexes': ('столбцы', 'не-уникальный'),
    'keys': ('на что', 'правила'),
}

# Виды находок, требующие внимания в первую очередь: они ломают запрос, а не
# замедляют его. ⚠ Порядок в отчёте задаётся этим списком.
MYSQL_SCHEMA_DIFF_HEAVY = ('нет', 'тип', 'сличение', 'null')


def mysql_schema_diff(left, right) -> list[dict]:
    """Сравнить схемы двух баз: столбцы, типы, сличения, индексы, связи.

    Args:
        left: снимок от `mysql_schema_diff_read` — своя сторона.
        right: снимок другой базы — та, куда выкладывают.

    Returns:
        list[dict]: записи `{kind, what, where, left, right}` — вид (`columns`,
        `indexes`, `keys`), что разошлось (`нет`, `тип`, `сличение`…), место и оба
        значения. Пусто — устройство совпадает.

    ⚠ Тяжёлые находки идут первыми (`MYSQL_SCHEMA_DIFF_HEAVY`): отсутствующий
    столбец и разное сличение ломают запрос, а недостающий индекс лишь замедляет.
    """
    out = []

    for kind, fields in MYSQL_SCHEMA_DIFF_FIELDS.items():
        here, there = left.get(kind, {}), right.get(kind, {})

        for name in sorted(set(here) | set(there)):
            if name not in there:
                out.append(_note(kind, 'нет', name, 'есть', 'нет'))
                continue
            if name not in here:
                out.append(_note(kind, 'нет', name, 'нет', 'есть'))
                continue

            for index, field in enumerate(fields):
                was, now = here[name][index], there[name][index]
                if was != now:
                    out.append(_note(kind, field, name, was, now))

    heavy = {one: index for index, one in enumerate(MYSQL_SCHEMA_DIFF_HEAVY)}

    return sorted(out, key=lambda one: (heavy.get(one['what'], len(heavy)),
                                        one['kind'], one['where']))


def mysql_schema_diff_read(source) -> dict:
    """Снимок устройства базы: столбцы, индексы, связи.

    Args:
        source: команда, принимающая SQL на вход и печатающая ответ строками
            (как у `mysql_collate_of`): `docker exec -i db mysql -N base`.

    Returns:
        dict: `{вид: {имя: (значения…)}}` по видам из `MYSQL_SCHEMA_DIFF_SQL`.

    Raises:
        RuntimeError: база не ответила. ⚠ Здесь бросаем, а не возвращаем пусто:
            пустой снимок сравнился бы с полным и объявил, что «на проде нет
            ничего» — отчёт на тысячу строк, весь неверный.
    """
    out = {}

    for kind, sql in MYSQL_SCHEMA_DIFF_SQL.items():
        # ⚠ `shell=True` намеренно, как в `mysql_collate_of` и `mysql_rehearse_run`:
        # `source` — команда оператора с `docker exec` и ssh, где кавычки идут в три
        # слоя. Строка приходит из своей же командной строки, чужого ввода здесь нет.
        got = subprocess.run(str(source), shell=True, input=sql.strip(),
                             capture_output=True, text=True, check=False)

        if got.returncode != 0:
            raise RuntimeError(f'{kind}: база не ответила — '
                               f'{(got.stderr or "").strip()[:200]}')

        rows = {}
        for line in got.stdout.split('\n'):
            if not line.strip():
                continue
            parts = line.split('\t')
            rows[parts[0]] = tuple(parts[1:])

        out[kind] = rows

    return out


def mysql_schema_diff_format(found, show=40) -> str:
    """Отчёт словами. Пусто — устройство баз совпадает."""
    if not found:
        return 'Устройство баз совпадает.'

    heavy = sum(1 for one in found if one['what'] in MYSQL_SCHEMA_DIFF_HEAVY)
    out = [f'Расхождений: {len(found)}, из них ломающих запрос: {heavy}.']
    kind_now = ''

    for one in found[:show]:
        if one['kind'] != kind_now:
            kind_now = one['kind']
            out.append(f'\n── {kind_now}')

        out.append(f"  {one['what']:12} {one['where']:44} "
                   f"{one['left']} │ {one['right']}")

    if len(found) > show:
        out.append(f'\n… и ещё {len(found) - show}')

    return '\n'.join(out)


def main() -> int:
    """CLI: код возврата 1 — есть ломающие запрос расхождения; 0 — нет."""
    import argparse

    ap = argparse.ArgumentParser(
        description='Чем устройство одной базы отличается от другой.')
    ap.add_argument('--left', required=True, help='команда своей базы')
    ap.add_argument('--right', required=True, help='команда той базы')
    ap.add_argument('--full', action='store_true', help='все расхождения')
    ns = ap.parse_args()

    try:
        found = mysql_schema_diff(mysql_schema_diff_read(ns.left),
                                  mysql_schema_diff_read(ns.right))
    except RuntimeError as bad:
        print(f'Сравнить не вышло: {bad}')

        return 2

    print(mysql_schema_diff_format(found, show=10_000 if ns.full else 40))

    return 1 if any(one['what'] in MYSQL_SCHEMA_DIFF_HEAVY for one in found) else 0


def _note(kind, what, where, left, right) -> dict:
    """Одна находка: вид, что разошлось, место и оба значения."""
    return {'kind': kind, 'what': what, 'where': where,
            'left': left or '—', 'right': right or '—'}


if __name__ == '__main__':
    sys.exit(main())

"""
Сличения столбцов: где базы разошлись и какой запрос на этом упадёт.

Сличение (collation) задаётся при создании базы и переживает её годами. Прод,
заведённый на MySQL 8, получает `utf8mb4_0900_ai_ci`; база, поднятая из старого
дампа или на MariaDB, — `utf8mb4_general_ci`. Данные одинаковые, а запрос,
работающий там, здесь падает.

## Чем это проявляется

    ERROR 1267 (HY000): Illegal mix of collations
    (utf8mb4_general_ci,IMPLICIT) and (utf8mb4_0900_ai_ci,IMPLICIT)

Сообщение называет два сличения и молчит о том, **какое из них чьё** и что с этим
делать. Отсюда и цена: миграция падает не на проде и не на своей базе, а на
третьей — и ровно в тот момент, когда её накатывают.

## ⚠ Три места, где это ломается

- **столбец против пользовательской переменной** (`WHERE name = CONCAT(…, @x)`):
  у `@x` та же «весомость», что у столбца, и MySQL отказывается выбирать между
  ними;
- **временная таблица**: берёт сличение **базы**, а не таблицы-источника, и
  соединение с ней падает там, где обычное соединение работает;
- **`UNION` разных таблиц**, если они заведены в разное время.

## ⚠ Чинить `COLLATE`-ом в миграции — плохо

Имя сличения подойдёт ровно одной базе, а миграция едет во все. Годные способы:
сравнивать числами (у чисел сличения нет), спрашивать имя у самой базы, либо
держать значение в переменной только для сборки строки, но не для сравнения.
"""
import argparse
import re
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Что ищем в тексте миграции: конструкции, падающие при разных сличениях.
# ⚠ Список неполон по природе — он ловит то, на чём уже спотыкались, а не всё
# возможное. Пополнять по следам живых отказов.
MYSQL_COLLATE_RISKS = (
    (r'=\s*CONCAT\([^)]*@\w+', 'сравнение столбца с CONCAT(…, @переменная)'),
    (r'LIKE\s+CONCAT\([^)]*@\w+', 'LIKE с CONCAT(…, @переменная)'),
    (r'CREATE\s+TEMPORARY\s+TABLE', 'временная таблица берёт сличение базы, '
                                    'а не таблицы-источника'),
    (r'\bUNION\b', 'UNION таблиц, заведённых в разное время'),
)


def mysql_collate_of(table: str, column: str = '', source: str = '') -> dict:
    """
    Сличения столбцов таблицы.

    Args:
        table: таблица.
        column: один столбец; пусто — все текстовые.
        source: пусто — своя база; иначе команда, печатающая TSV
            «столбец<TAB>сличение».

    Returns:
        `{столбец: сличение}`.

    Raises:
        RuntimeError: спросить не удалось.
    """
    if source:
        import subprocess

        # ⚠ `shell=True` намеренно: `source` — команда оператора с `docker exec`
        # и ssh, где кавычки идут в три слоя. Строка приходит из своей же
        # командной строки, чужого ввода здесь нет.
        got = subprocess.run(source, shell=True, capture_output=True,
                             text=True, timeout=120)
        if got.returncode:
            raise RuntimeError(f'источник не ответил: {got.stderr.strip()[:200]}')
        out = {}
        for line in got.stdout.splitlines():
            part = line.rstrip('\n').split('\t')
            if len(part) == 2 and part[1] not in ('NULL', ''):
                out[part[0]] = part[1]

        return out

    from mysql_.mysql_query import mysql_query_run

    sql = ("SELECT COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS"
           " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
           "   AND COLLATION_NAME IS NOT NULL")
    params = (table,)
    if column:
        sql += ' AND COLUMN_NAME = %s'
        params += (column,)

    return {row['COLUMN_NAME']: row['COLLATION_NAME']
            for row in mysql_query_run(sql, params)['rows']}


def mysql_collate_diff(left: dict, right: dict) -> list:
    """
    Столбцы, у которых сличения разошлись.

    Returns:
        Список `(столбец, слева, справа)`; пустой — расхождений нет.
    """
    return [(name, left.get(name, '—'), right.get(name, '—'))
            for name in sorted(set(left) | set(right))
            if left.get(name) != right.get(name)]


def mysql_collate_risks(sql_text: str) -> list:
    """
    Места в тексте миграции, падающие при разных сличениях.

    Args:
        sql_text: текст миграции.

    Returns:
        Список `(строка, кусок, чем опасно)`.

    ⚠ Ищем по **коду**, а не по всему файлу: то же `UNION` в шапке-объяснении не
    опасно, а шума даёт столько же, сколько настоящих находок.
    """
    body = _without_comments(sql_text)
    out = []
    for number, line in enumerate(body.splitlines(), start=1):
        for pattern, why in MYSQL_COLLATE_RISKS:
            if re.search(pattern, line, re.I):
                out.append((number, line.strip()[:70], why))

    return out


def mysql_collate_format(diff: list, risks: list, left: str = 'левая',
                         right: str = 'правая') -> str:
    """Отчёт словами: чем базы разошлись и что на этом упадёт."""
    lines = []

    if diff:
        lines.append('── сличения разошлись')
        for name, a, b in diff:
            lines.append(f'   {name:24} {left}: {a:22} {right}: {b}')
    else:
        lines.append('── сличения совпадают')

    if risks:
        lines.append('\n── места, падающие при разных сличениях')
        for number, text, why in risks:
            lines.append(f'   строка {number}: {why}')
            lines.append(f'      {text}')
        if diff:
            lines.append('\n⚠ И расхождение, и опасные места разом: запрос '
                         'упадёт на одной из баз.')
    elif diff:
        lines.append('\n⚠ Расхождение есть, но опасных мест в тексте не видно. '
                     'Проверка неполна: она ловит известные случаи, а не все.')

    return '\n'.join(lines)


def _without_comments(text: str) -> str:
    """SQL без комментариев `--`, `#` и `/* */`."""
    text = re.sub(r'/\*.*?\*/', ' ', str(text), flags=re.S)

    return '\n'.join(re.sub(r'(--|#).*$', '', line) for line in text.splitlines())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Сличения столбцов в двух базах и опасные места миграции.')
    parser.add_argument('table', help='таблица')
    parser.add_argument('--column', default='', help='один столбец')
    parser.add_argument('--other', default='',
                        help='команда, печатающая TSV «столбец<TAB>сличение» '
                             'из второй базы')
    parser.add_argument('--sql', default='',
                        help='файл миграции: проверить на опасные конструкции')
    args = parser.parse_args()

    try:
        here = mysql_collate_of(args.table, args.column)
        there = mysql_collate_of(args.table, args.column, args.other) \
            if args.other else {}
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

    risks = mysql_collate_risks(Path(args.sql).read_text(encoding='utf-8')) \
        if args.sql else []

    if not args.other:
        for name, value in sorted(here.items()):
            print(f'  {name:24} {value}')
        if risks:
            print()
            print(mysql_collate_format([], risks))
        raise SystemExit(0)

    print(mysql_collate_format(mysql_collate_diff(there, here), risks,
                              'та база', 'эта база'))

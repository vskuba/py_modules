"""
Миграция на копии боевой базы: прогнать дважды и показать, что вышло.

Миграцию нельзя проверить на своей базе: она отличается от прода данными, а
ломается миграция как раз на них. Ручной круг выглядит так — `mysqldump` через
ssh, `DROP DATABASE`, залить, применить, применить **второй раз**, сверить. Минут
двадцать, и каждый раз забывается какая-нибудь таблица.

## Что ловит

Замерено на живых миграциях, все четыре поломки нашлись здесь, а не на проде:

- `JSON_EXTRACT` падает на пустой `metadata` — у пяти шагов прода она не JSON, и
  миграция обрывается на середине;
- `%if talk_history%` не переименовался: замена искала `%talk_history%`, а перед
  именем стоит `%if `;
- якорь вставки промахнулся мимо главного шага;
- у столбцов разное сличение (`utf8mb4_general_ci` против `utf8mb4_0900_ai_ci`),
  и сравнение с переменной роняет запрос.

## ⚠ Второй прогон обязателен

Миграции применяются повторно чаще, чем кажется: восстановление базы, второй
контур, случайный перезапуск. Неидемпотентная либо падает, либо тихо удваивает
сделанное — и заметить это на проде уже некому.

## ⚠ Таблицы выводятся сами

По названным именам подтягиваются те, на которые они ссылаются внешними ключами:
без родительских таблиц заливка падает на `Cannot add or update a child row`, а
без справочников (`*_type`) — на вставке. Так и было: `agent_workflow_step_type`
забылся, и репетиция упала не на миграции, а на подготовке.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Сколько ждём снятия среза. Дамп нескольких таблиц через ssh идёт секунды, но
# канал бывает медленным, а обрыв на середине даёт негодную копию.
MYSQL_REHEARSE_DUMP_TIMEOUT = 600

# Сколько ждём саму миграцию. Долгая — обычно признак того, что она перебирает
# строки поштучно, и это стоит увидеть до прода.
MYSQL_REHEARSE_APPLY_TIMEOUT = 900


def mysql_rehearse_tables(tables: list, dump_cmd: str) -> list:
    """
    Дополнить список таблиц теми, без которых копия не зальётся.

    Args:
        tables: что назвал человек.
        dump_cmd: команда, печатающая схему (нужна, чтобы спросить ключи).

    Returns:
        Список таблиц, включая родительские по внешним ключам.

    ⚠ Спрашиваем **у источника**, а не у своей базы: набор таблиц у них может
    расходиться, и достроить по своей — значит недосчитаться чужих.
    """
    # ⚠ Имена таблиц уезжают **в текст запроса**, а не подстановкой: этого не
    # умеет ни одна база. Поэтому отсекаем всё, что не похоже на имя таблицы —
    # подстановки здесь нет, а склейка есть.
    want = [one.strip() for one in tables
            if one.strip() and _name_ok(one.strip())]
    if not want:
        return []

    names = "', '".join(want)
    sql = ("SELECT DISTINCT REFERENCED_TABLE_NAME FROM"
           " information_schema.KEY_COLUMN_USAGE"
           f" WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ('{names}')"
           "   AND REFERENCED_TABLE_NAME IS NOT NULL")
    got = _run(dump_cmd.replace('{sql}', sql), MYSQL_REHEARSE_DUMP_TIMEOUT)
    extra = [line.strip() for line in got.splitlines() if line.strip()]

    return sorted(set(want) | set(extra))


def mysql_rehearse_run(migration: str, dump_cmd: str, apply_cmd: str,
                       reset_cmd: str) -> dict:
    """
    Прогнать миграцию на свежей копии дважды.

    Args:
        migration: путь к файлу миграции.
        dump_cmd: команда, печатающая дамп нужных таблиц.
        apply_cmd: команда, принимающая SQL на вход (в неё уйдёт и дамп, и
            миграция).
        reset_cmd: команда, заводящая пустую базу заново.

    Returns:
        `{'clean': вывод первого прогона, 'again': вывод второго,
          'idempotent': совпали ли они}`.

    Raises:
        RuntimeError: подготовка или прогон не удались.

    ⚠ Совпадение выводов — **признак, а не доказательство** идемпотентности:
    миграция может печатать одно, а делать разное. Но расхождение выводов
    означает беду всегда, и ловится оно здесь бесплатно.
    """
    text = Path(migration).read_text(encoding='utf-8')

    _run(reset_cmd, MYSQL_REHEARSE_DUMP_TIMEOUT)
    dump = _run(dump_cmd.replace('{sql}', ''), MYSQL_REHEARSE_DUMP_TIMEOUT)
    if not dump.strip():
        raise RuntimeError('дамп пуст: проверьте команду снятия среза')
    _feed(apply_cmd, dump)

    clean = _feed(apply_cmd, text)
    again = _feed(apply_cmd, text)

    return {'clean': clean, 'again': again,
            'idempotent': _norm(clean) == _norm(again)}


def mysql_rehearse_format(result: dict) -> str:
    """Отчёт о репетиции словами."""
    mark = 'да' if result.get('idempotent') else '⚠ НЕТ — второй прогон дал иное'

    return ('── чистый прогон\n' + (result.get('clean') or '(без вывода)')
            + '\n\n── повтор\n' + (result.get('again') or '(без вывода)')
            + f'\n\nидемпотентна: {mark}')


def _name_ok(name: str) -> bool:
    """Похоже ли на имя таблицы: буквы, цифры, подчёркивание, точка схемы.

    ⚠ Пропущенная кавычка здесь — не «неаккуратность», а чужой SQL в запросе к
    боевой схеме. Имя таблицы подстановкой не передашь, поэтому проверка на
    входе — единственная защита.
    """
    return bool(re.fullmatch(r'[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)?', str(name)))


def _run(command: str, timeout: int) -> str:
    """Выполнить команду оболочки и вернуть её вывод.

    ⚠ `shell=True` намеренно: сюда приходят команды с `docker exec` и ssh, где
    кавычки идут в три слоя. Строка берётся из своей же командной строки, чужого
    ввода здесь нет.
    """
    got = subprocess.run(command, shell=True, capture_output=True,
                         text=True, timeout=timeout)
    if got.returncode:
        raise RuntimeError(f'{command[:60]}…: {got.stderr.strip()[:300]}')

    return got.stdout


def _feed(command: str, payload: str) -> str:
    """Скормить SQL команде на вход и вернуть её вывод вместе с ошибками.

    ⚠ Ошибку **не бросаем**: отказ миграции — это и есть то, ради чего
    репетиция; его надо показать человеку, а не спрятать под исключением.
    """
    got = subprocess.run(command, shell=True, input=payload,
                         capture_output=True, text=True,
                         timeout=MYSQL_REHEARSE_APPLY_TIMEOUT)

    return (got.stdout or '') + (got.stderr or '')


def _norm(text: str) -> str:
    """Вывод без пустых строк и краевых пробелов — для сравнения прогонов."""
    return '\n'.join(line.rstrip() for line in str(text).splitlines()
                     if line.strip())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Прогнать миграцию на свежей копии боевой базы дважды.')
    parser.add_argument('migration', help='файл миграции')
    parser.add_argument('--dump', required=True,
                        help='команда, печатающая дамп; `{sql}` в ней заменится '
                             'на служебный запрос при --tables')
    parser.add_argument('--apply', required=True,
                        help='команда, принимающая SQL на вход')
    parser.add_argument('--reset', required=True,
                        help='команда, заводящая пустую базу заново')
    parser.add_argument('--tables', default='',
                        help='таблицы через запятую: покажет, каких не хватает '
                             'по внешним ключам, и ничего не прогонит')
    args = parser.parse_args()

    try:
        if args.tables:
            print(' '.join(mysql_rehearse_tables(args.tables.split(','), args.dump)))
            raise SystemExit(0)

        print(mysql_rehearse_format(
            mysql_rehearse_run(args.migration, args.dump, args.apply, args.reset)))
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

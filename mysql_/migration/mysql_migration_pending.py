"""
Что в самом деле поедет на эту базу: непримененные миграции и что они с ней сделают.

Перед выкладкой нужен ответ не «сколько файлов в каталоге», а «какие из них
применятся **на той** базе и что они там изменят». Ответ этот собирается руками
каждый раз заново — список файлов, список `migration_id` из `_yoyo_migration`,
разница, и глазами по текстам в поисках `DELETE`.

    PYTHONPATH=py_modules python -m mysql_.migration.mysql_migration_pending \\
        migrations --source "ssh prod 'docker exec -i db mysql -N base'"

## ⚠⚠ yoyo не спускается в подкаталоги — и на это здесь опираются

Проверено опытом: файлы в `migrations/local/` не применяются **никогда**. Это не
недоделка, а предохранитель: разовые загрузки данных, которым на проде делать
нечего, кладут туда, и они физически не могут туда уехать.

Поэтому подкаталоги показываются отдельной строкой «не поедет». Ошибиться здесь
можно в обе стороны, и обе дороги: счесть подкаталог рабочим — ждать миграцию,
которой не будет; счесть корень подкаталогом — увезти на прод разовую загрузку.

## ⚠⚠ Опасные места называются, но не запрещаются

`DELETE`, `TRUNCATE`, `DROP`, `UPDATE` без `WHERE` — это то, из-за чего выкладку
стоит делать с бэкапом и глазами, а не то, чего в миграциях не бывает. Инструмент
их **называет**; решать — человеку.

⚠ Ищутся по коду без комментариев: слово `DROP` в шапке-объяснении (а объяснения
здесь длинные) не опасно, а шума даёт больше, чем настоящие находки.

## ⚠ Состояние читается с той базы, о которой спрашивают

`source` — команда, принимающая SQL на вход и печатающая ответ (как у
`mysql_collate_of` и `mysql_rehearse_run`). Пусто — сверки с базой нет вовсе, и
«поедет» значит лишь «лежит в корне каталога»: разница между «не применена» и
«не видно базы» должна быть видна в отчёте, а не додумываться.
"""
import re
import subprocess
import sys

from pathlib import Path

# Файл запускают и путём. Тогда первым в путях лежит каталог файла, и `import mysql_`
# находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mysql_.migration.mysql_migration_check import mysql_migration_check_check

# Таблица состояния yoyo и столбец, в котором лежит имя применённого файла без `.sql`.
MYSQL_MIGRATION_PENDING_TABLE = '_yoyo_migration'
MYSQL_MIGRATION_PENDING_COLUMN = 'migration_id'

# Опасные места: что именно делает миграцию требующей бэкапа и внимания.
#
# ⚠ `UPDATE` без `WHERE` отдельной приметой: сам по себе `UPDATE` в миграции —
# обычное дело, а без условия он переписывает таблицу целиком.
MYSQL_MIGRATION_PENDING_DANGER = (
    (r'\bTRUNCATE\s+(?:TABLE\s+)?`?(\w+)', 'TRUNCATE {0} — таблица чистится целиком'),
    (r'\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?`?(\w+)', 'DROP TABLE {0}'),
    (r'\bDROP\s+DATABASE\b', 'DROP DATABASE — база сносится'),
    (r'\bDELETE\s+(?:\w+\s+)?FROM\s+`?(\w+)', 'DELETE FROM {0}'),
    (r'\bUPDATE\s+`?(\w+)`?\s+SET\b(?![^;]*\bWHERE\b)',
     'UPDATE {0} SET без WHERE — переписывает все строки'),
    (r'\bDROP\s+(?:COLUMN\s+)?`?(\w+)`?\s*(?:,|;|$)', 'DROP COLUMN {0}'),
    (r'\bSET\s+FOREIGN_KEY_CHECKS\s*=\s*0',
     'FOREIGN_KEY_CHECKS = 0 — правила связей не сработают'),
)

# Комментарии SQL: в этих проектах шапка миграции — страница текста, и искать по ней
# опасные слова бессмысленно.
_COMMENT_LINE = re.compile(r'--[^\n]*')
_COMMENT_BLOCK = re.compile(r'/\*.*?\*/', re.S)


def mysql_migration_pending(migrations_dir, applied=(), source='') -> list[dict]:
    """Миграции, которые применятся на базе, и что они с ней сделают.

    Args:
        migrations_dir: каталог с `*.sql`.
        applied: применённые `migration_id`; пусто — сверки с базой не было.
        source: команда, принимающая SQL и печатающая ответ; задана — `applied`
            снимается с базы ею, а довод `applied` не нужен.

    Returns:
        list[dict]: записи `{file, label, runs, danger, why}` в порядке применения.
        `runs` — поедет ли (ложно у применённых и у лежащих в подкаталоге), `danger`
        — список опасных мест словами, `why` — почему не поедет.

    ⚠⚠ Файлы подкаталогов в ответе **есть**, но с `runs = False`: знать, что они
    существуют и не поедут, важнее, чем не видеть их вовсе — иначе разовая загрузка
    данных выглядит потерянной.

    ⚠ Сверка с базой не состоялась (нет `source`, база недоступна) — `runs` истинно
    у всех корневых файлов, включая уже применённые. Отчёт скажет об этом прямо.
    """
    where = Path(migrations_dir)
    known = set(mysql_migration_pending_applied(source) if source else applied)
    out = []

    for one in sorted(where.rglob('*.sql')):
        label = one.stem
        deep = one.parent != where

        if deep:
            why = f'подкаталог {one.parent.name}/ — yoyo в подкаталоги не спускается'
        elif label in known:
            why = 'уже применена'
        else:
            why = ''

        out.append({'file': str(one.relative_to(where)), 'label': label,
                    'runs': not why, 'why': why,
                    'danger': _danger_of(one.read_text(encoding='utf-8',
                                                       errors='replace'))})

    return out


def mysql_migration_pending_applied(source) -> list[str]:
    """Применённые `migration_id` с базы, о которой спрашивают.

    Args:
        source: команда, принимающая SQL на вход и печатающая ответ строками.

    Returns:
        list[str]: имена применённых миграций. Пусто — таблицы состояния ещё нет
        (база чистая) либо база не ответила.

    ⚠⚠ Пустой ответ двусмыслен, и различить эти случаи здесь нельзя: `mysql -N` на
    чистой базе и он же при ошибке доступа печатают одинаково мало. Поэтому
    вызывающий обязан сказать в отчёте «сверка не состоялась» — сам по себе пустой
    список значит только «применённых не видно».
    """
    sql = (f'SELECT {MYSQL_MIGRATION_PENDING_COLUMN} '
           f'FROM {MYSQL_MIGRATION_PENDING_TABLE}')
    # ⚠ `shell=True` намеренно, как в `mysql_collate_of` и `mysql_rehearse_run`:
    # `source` — команда оператора с `docker exec` и ssh, где кавычки идут в три
    # слоя. Строка приходит из своей же командной строки, чужого ввода здесь нет.
    got = subprocess.run(str(source), shell=True, input=sql,
                         capture_output=True, text=True, check=False)

    if got.returncode != 0:
        return []

    return [one.strip() for one in got.stdout.split('\n') if one.strip()]


def mysql_migration_pending_format(rows, checked=True) -> str:
    """Отчёт словами: что поедет, что нет и где опасные места."""
    runs = [one for one in rows if one['runs']]
    held = [one for one in rows if not one['runs']]
    out = [f'Поедет миграций: {len(runs)} из {len(rows)}.']

    if not checked:
        out.append('⚠ Сверки с базой не было — «поедет» значит «лежит в корне '
                   'каталога», а не «не применена».')

    for one in runs:
        out.append(f"\n  → {one['file']}")
        for bad in one['danger']:
            out.append(f'      ⚠ {bad}')

    deep = [one for one in held if 'подкаталог' in one['why']]
    if deep:
        out.append(f'\nНе поедет — подкаталоги ({len(deep)}):')
        for one in deep:
            out.append(f"  · {one['file']}")

    done = len(held) - len(deep)
    if done:
        out.append(f'\nУже применено: {done}.')

    risky = sum(len(one['danger']) for one in runs)
    if risky:
        out.append(f'\n⚠⚠ Опасных мест в уезжающем: {risky}. Бэкап до записи.')

    return '\n'.join(out)


def main() -> int:
    """CLI: код возврата 1 — в уезжающем есть опасные места; 0 — чисто."""
    import argparse

    ap = argparse.ArgumentParser(
        description='Что в самом деле поедет на базу и что оно с ней сделает.')
    ap.add_argument('migrations', help='каталог с *.sql')
    ap.add_argument('--source', default='',
                    help='команда, принимающая SQL и печатающая ответ')
    ap.add_argument('--check', action='store_true',
                    help='заодно проверить каталог (mysql_migration_check)')
    ns = ap.parse_args()

    rows = mysql_migration_pending(ns.migrations, source=ns.source)
    print(mysql_migration_pending_format(rows, checked=bool(ns.source)))

    if ns.check:
        bad = mysql_migration_check_check(ns.migrations)
        print(f'\nНаходок по каталогу: {len(bad)}'
              + (' — см. mysql_migration_check' if bad else ''))

    return 1 if any(one['danger'] for one in rows if one['runs']) else 0


def _danger_of(text: str) -> list[str]:
    """Опасные места в тексте миграции — по коду, без комментариев."""
    body = _COMMENT_LINE.sub(' ', _COMMENT_BLOCK.sub(' ', text))
    out = []

    for pattern, said in MYSQL_MIGRATION_PENDING_DANGER:
        for got in re.finditer(pattern, body, re.IGNORECASE):
            said_now = said.format(*(got.groups() or ('',)))
            if said_now not in out:
                out.append(said_now)

    return out


if __name__ == '__main__':
    sys.exit(main())

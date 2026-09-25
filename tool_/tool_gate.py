"""
Застава перед написанием кода: сперва посмотри, нет ли этого в общем слое.

Работает хуком Claude Code на `Write`/`Edit`, общим для всех проектов. Видит текст,
который собираются записать, ищет в нём приметы ремесла (`tool_instead`) и, если
нашла, **один раз отменяет запись**, показав, чем это уже делают.

## ⚠⚠ Зачем застава, когда есть поиск

`tool_find` существует давно, стоит первым правилом в `CLAUDE.md` и работает
полторы секунды. И всё равно за одну сессию поверх готового были написаны: своя
обёртка над `ssh` на прод (есть `ssh_/ssh_x.py`), свой разовый запрос к базе (есть
`mysql_/mysql_query.py`), свой поиск кириллицы в именах (есть
`variable_/variable_cyrillic.py`), своя репетиция миграции (есть
`mysql_/mysql_rehearse.py`) и свой снимок страницы (есть `web_/web_drive_page.py`).

Правило не сработало ни разу — не потому, что непонятно, а потому, что **звать
поиск надо помнить**, а пишущий код в этот момент занят другим. Напоминание,
зависящее от памяти того, кому оно адресовано, не работает по устройству.

Отсюда застава: она не просит помнить, она **встаёт на пути**.

## ⚠⚠ Отменяет один раз, а не держит

Вторая запись того же файла проходит. Это главное в устройстве:

    первая попытка  → отмена + список кандидатов
    вторая попытка  → пропуск

Застава, отменяющая всегда, — это застава, которую снимут: писать-то надо. Её
задача — не запретить своё, а не дать написать его **не посмотрев**. Посмотрел и
решил, что не то (половина «не тех» оказывается теми — но не все), — пиши.

⚠ Отсюда «виделки» хранятся на диске (`TOOL_GATE_SEEN`) и живут сутки: через день
та же запись снова покажет кандидатов. Файл потерялся — потеряется и память о
показанном, и одна лишняя отмена. Это дешевле, чем обратное.

## Чего застава не делает

⚠ Не смотрит на правки в самом `py_modules`: там приметы стоят в списке примет,
и застава блокировала бы собственную починку.

⚠ Не смотрит на прозу — `.md`, `.txt`, `.json`, `.html`: примета в тексте
документации не значит ничего, а шума даёт столько же.

⚠ Не решает, дублирование ли это. `tool_instead` отдаёт **кандидатов**; смотрит
человек или агент. Застава лишь обеспечивает, что смотрят.
"""
import json
import os
import sys
import time

from pathlib import Path

# Файл запускают и путём — из хука он зовётся именно так. Тогда первым в путях лежит
# каталог файла, и `import tool_` находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_.tool_instead import tool_instead, tool_instead_format

# Где помнится показанное. ⚠ Не в проекте: застава общая, а проектов много.
TOOL_GATE_SEEN = Path.home() / '.cache' / 'tool_gate_seen.json'

# Сколько живёт память о показанном. ⚠ Сутки: за сессию застава не должна мешать
# дважды, а через день о кандидатах стоит напомнить — файл с тех пор изменился.
TOOL_GATE_SEEN_FOR = 24 * 60 * 60

# Код возврата, которым Claude Code отменяет вызов и отдаёт stderr агенту.
TOOL_GATE_BLOCK = 2

# Переменная окружения, снимающая заставу целиком.
TOOL_GATE_PASS = 'TOOL_GATE_PASS'

# Что застава смотрит: код, и только его.
TOOL_GATE_SUFFIXES = ('.py', '.sh', '.sql', '.js')

# Пути, на которые застава не смотрит. ⚠⚠ Сам общий слой — в первую очередь: там
# приметы лежат списком примет, и застава не пускала бы собственную починку.
TOOL_GATE_BLIND = ('py_modules/', '/py_modules', 'scratchpad/', '/.venv/',
                   'node_modules/', 'migrations/')


def tool_gate(path, code, seen_for=TOOL_GATE_SEEN_FOR) -> dict:
    """Пропускать ли эту запись.

    Args:
        path: куда пишут.
        code: что пишут — текст целиком либо новый кусок правки.
        seen_for: сколько секунд помнить, что кандидаты уже показаны.

    Returns:
        dict: `{block, found, why}` — отменять ли запись, находки `tool_instead` и
        строка о решении. `block` ложно при любом из: примет нет, путь слепой,
        кандидаты по этому файлу уже показаны, застава снята.

    ⚠⚠ Второй вызов по тому же файлу пропускает — см. ⚠⚠ в докстринге модуля.
    Память о показанном пишется **здесь же**, а не вызывающим: забудь он это
    сделать, и застава встала бы насмерть.
    """
    where = str(path or '')

    if os.environ.get(TOOL_GATE_PASS):
        return {'block': False, 'found': [], 'why': f'снята через {TOOL_GATE_PASS}'}

    if not where.endswith(TOOL_GATE_SUFFIXES):
        return {'block': False, 'found': [], 'why': 'не код'}

    if any(one in where for one in TOOL_GATE_BLIND):
        return {'block': False, 'found': [], 'why': 'слепой путь'}

    found = tool_instead(code)
    if not found:
        return {'block': False, 'found': [], 'why': 'примет нет'}

    if _seen(where, seen_for):
        return {'block': False, 'found': found, 'why': 'кандидаты уже показаны'}

    _remember(where)

    return {'block': True, 'found': found, 'why': 'кандидаты показаны впервые'}


def tool_gate_format(verdict) -> str:
    """Замечание для агента: что уже умеет слой и что теперь делать."""
    if not verdict.get('block'):
        return ''

    return '\n'.join((
        'Запись отменена заставой общего слоя — на один раз.',
        '',
        tool_instead_format(verdict['found']),
        '',
        'Что сделать: прочитать докстринг подходящего кандидата целиком (половина',
        '«не тех» оказывается теми) и либо взять его, либо повторить запись — вторая',
        'попытка проходит. Ничего не подошло — уточнить словами:',
        '    PYTHONPATH=py_modules python -m tool_.tool_find "<что делаете>"',
    ))


def main() -> int:
    """Хук `PreToolUse` на `Write`/`Edit`: код 2 отменяет запись, 0 — пропускает.

    ⚠ Своя ошибка пропускает запись: сломанный хук, запрещающий писать, останавливает
    работу целиком и необъяснимо. Непроверенная запись — меньшая беда.
    """
    data = _payload()
    if data.get('tool_name') not in ('Write', 'Edit', 'NotebookEdit'):
        return 0

    where, code = _written(data)

    try:
        verdict = tool_gate(where, code)
    except Exception as bad:      # noqa: BLE001 — см. ⚠ выше
        print(f'застава слоя сорвалась, запись пропущена: {bad}', file=sys.stderr)

        return 0

    if not verdict['block']:
        return 0

    print(tool_gate_format(verdict), file=sys.stderr)

    return TOOL_GATE_BLOCK


def _seen(where: str, seen_for: int) -> bool:
    """Показывали ли кандидатов по этому файлу недавно."""
    known = _load()
    when = known.get(where, 0)

    return bool(when) and (time.time() - when) < seen_for


def _remember(where: str) -> None:
    """Запомнить, что кандидаты по файлу показаны.

    ⚠ Ошибка записи проглатывается: не суметь запомнить — значит показать
    кандидатов лишний раз, а упасть здесь значит отменить чужую запись без
    объяснения. Первое — неудобство, второе — поломка.
    """
    known = {one: when for one, when in _load().items()
             if (time.time() - when) < TOOL_GATE_SEEN_FOR}
    known[where] = int(time.time())

    try:
        TOOL_GATE_SEEN.parent.mkdir(parents=True, exist_ok=True)
        TOOL_GATE_SEEN.write_text(json.dumps(known), encoding='utf-8')
    except OSError:
        pass


def _load() -> dict:
    """Память о показанном. Нет файла или он битый — пустая память."""
    try:
        got = json.loads(TOOL_GATE_SEEN.read_text(encoding='utf-8'))

        return got if isinstance(got, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _payload() -> dict:
    """Ввод хука: JSON на stdin. Не JSON — пустой словарь, и застава молчит."""
    try:
        return json.loads(sys.stdin.read() or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _written(data: dict) -> tuple:
    """Куда и что пишут — из полей `Write`, `Edit` и `NotebookEdit`.

    ⚠ У `Edit` смотрим **новый** кусок, а не файл целиком: примета из старого кода
    отменяла бы правку любой строки в файле, написанном когда-то давно.
    """
    got = data.get('tool_input') or {}
    where = got.get('file_path') or got.get('notebook_path') or ''
    code = got.get('content') or got.get('new_string') or got.get('new_source') or ''

    return where, code


if __name__ == '__main__':
    sys.exit(main())

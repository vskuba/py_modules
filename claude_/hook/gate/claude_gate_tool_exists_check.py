"""
Застава перед написанием кода: сперва посмотри, нет ли этого в общем слое.

Работает хуком Claude Code на `Write`/`Edit`, общим для всех проектов. Видит текст,
который собираются записать, ищет в нём приметы ремесла (`tool_instead`) и, если
нашла, **один раз отменяет запись**, показав, чем это уже делают.

## ⚠⚠ Зачем застава, когда есть поиск

Поиск по намерению существует давно, стоит первым правилом в `CLAUDE.md` и работает
полторы секунды. И всё равно за одну сессию поверх готового были написаны пять своих
обёрток: к удалённому стеку, к разовому запросу в базу, к поиску кириллицы в именах,
к репетиции миграции и к снимку страницы. Каждая находилась поиском первой строкой.

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

⚠ Память о показанном живёт сутки и лежит на диске (`CLAUDE_GATE_TOOL_EXISTS_CHECK_SEEN`): через
день та же запись снова покажет кандидатов. Файл потерялся — потеряется и память, и
одна лишняя отмена. Это дешевле, чем обратное. Устройство памяти — `claude_gate`.

## Чего застава не делает

⚠⚠ Не смотрит на общий слой и на собственный дом. Причина у обоих одна: там приметы
перечислены **списком примет**, и застава отменяла бы собственную починку. Проверено
дорогой ценой — первая же запись этого файла на новом месте была отменена им самим.

⚠ Не смотрит на прозу — `.md`, `.txt`, `.json`, `.html`: примета в тексте
документации не значит ничего, а шума даёт столько же.

⚠ Не решает, дублирование ли это. `tool_instead` отдаёт **кандидатов**; смотрит
человек или агент. Застава лишь обеспечивает, что смотрят.
"""
import sys
from pathlib import Path

# ⚠ Загрузка через `spec_from_file_location` (так делает сюита) не кладёт корень
# слоя в путь поиска сама, а запуск файла по пути — кладёт через sys.path[0].
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from claude_.hook.claude_hook import claude_hook_run                               # noqa: E402
from claude_.hook.gate.claude_gate import (claude_gate_asked, claude_gate_forget,  # noqa: E402
                                      claude_gate_off, claude_gate_remember)
from tool_.tool_instead import tool_instead, tool_instead_format              # noqa: E402

# Где помнится показанное. ⚠ Не в проекте: застава общая, а проектов много.
CLAUDE_GATE_TOOL_EXISTS_CHECK_SEEN = Path.home() / '.cache' / 'claude_gate_tool_exists_check_seen.json'

# Сколько отмен на один файл. Одна: см. ⚠⚠ «отменяет один раз» выше.
CLAUDE_GATE_TOOL_EXISTS_CHECK_ASKS = 1

# Инструменты, на которые поставлена застава.
CLAUDE_GATE_TOOL_EXISTS_CHECK_TOOLS = ('Write', 'Edit', 'NotebookEdit')

# Переменная окружения, снимающая заставу целиком. ⚠ Имя своё и длинное: клапан —
# договор с человеком, он записан в документации и нужен в три часа ночи. Чужое
# короткое имя из чужого неймспейса теряется в выводе `env` рядом с десятками чужих.
CLAUDE_GATE_TOOL_EXISTS_CHECK_PASS = 'CLAUDE_GATE_TOOL_EXISTS_CHECK_PASS'

# Что застава смотрит: код, и только его.
CLAUDE_GATE_TOOL_EXISTS_CHECK_SUFFIXES = ('.py', '.sh', '.sql', '.js')

# Пути, на которые застава не смотрит.
#
# ⚠⚠ Общий слой и собственный дом застав — в первую очередь: в обоих приметы лежат
# списком примет. Дом берётся от `__file__`, а не строкой: застава обязана быть слепа
# к себе, куда бы её ни положили.
CLAUDE_GATE_TOOL_EXISTS_CHECK_BLIND = ('py_modules/', '/py_modules', 'scratchpad/', '/.venv/',
                          'node_modules/', 'migrations/',
                          f'{Path(__file__).resolve().parent}/')


def claude_gate_tool_exists_check(path, code, seen_path=None, now=None) -> dict:
    """Пропускать ли эту запись.

    Args:
        path: куда пишут.
        code: что пишут — текст целиком либо новый кусок правки.
        seen_path: где лежит память; пусто — `CLAUDE_GATE_TOOL_EXISTS_CHECK_SEEN`.
        now: epoch «сейчас»; только для тестов.

    Returns:
        dict: `{block, found, why}` — отменять ли запись, находки `tool_instead` и
        строка о решении. `block` ложно при любом из: примет нет, путь слепой,
        кандидаты по этому файлу уже показаны, застава снята.

    ⚠⚠ Второй вызов по тому же файлу пропускает — см. ⚠⚠ в докстринге модуля.
    Память о показанном пишется **здесь же**, а не вызывающим: забудь он это
    сделать, и застава встала бы насмерть.

    ⚠ Путь памяти берётся в момент вызова, а не в момент объявления: привязанный
    аргументом файл нельзя подменить в тесте, и проверка натыкалась бы на живую
    память хозяина.
    """
    where = str(path or '')
    seen = Path(seen_path or CLAUDE_GATE_TOOL_EXISTS_CHECK_SEEN)

    if claude_gate_off(CLAUDE_GATE_TOOL_EXISTS_CHECK_PASS):
        return {'block': False, 'found': [], 'why': f'снята через {CLAUDE_GATE_TOOL_EXISTS_CHECK_PASS}'}

    if not where.endswith(CLAUDE_GATE_TOOL_EXISTS_CHECK_SUFFIXES):
        return {'block': False, 'found': [], 'why': 'не код'}

    if any(one in where for one in CLAUDE_GATE_TOOL_EXISTS_CHECK_BLIND):
        return {'block': False, 'found': [], 'why': 'слепой путь'}

    found = tool_instead(code)

    if not found:
        # ⚠ Эпизод закрыт: примет в файле больше нет. Вернутся завтра — застава снова
        # отменит один раз, а не промолчит по вчерашней памяти.
        claude_gate_forget(where, seen)

        return {'block': False, 'found': [], 'why': 'примет нет'}

    if claude_gate_asked(where, seen, now=now) >= CLAUDE_GATE_TOOL_EXISTS_CHECK_ASKS:
        return {'block': False, 'found': found, 'why': 'кандидаты уже показаны'}

    claude_gate_remember(where, seen, now=now)

    return {'block': True, 'found': found, 'why': 'кандидаты показаны впервые'}


def claude_gate_tool_exists_check_format(verdict) -> str:
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

    ⚠ Разбор ввода, коды возврата и «при своей поломке пропускать» — в `claude_hook`;
    клапан, бюджет и память — в `claude_gate`. Здесь только вопрос: что за запись и
    стоит ли её задержать.
    """
    return claude_hook_run(CLAUDE_GATE_TOOL_EXISTS_CHECK_TOOLS, _look)


def _look(data: dict) -> str:
    """Замечание по этой записи либо пусто. Вид проверки для `claude_hook_run`."""
    where, code = _written(data)

    return claude_gate_tool_exists_check_format(claude_gate_tool_exists_check(where, code))


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

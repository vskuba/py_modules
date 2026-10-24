"""
Обвязка хука Claude Code: разобрать ввод, ответить кодом возврата — одним местом на все заставы.

Хук получает на stdin JSON с именем инструмента и его доводами, а отвечает **кодом
возврата**: `0` пропускает вызов, `2` отменяет его и отдаёт `stderr` агенту
замечанием. Договор короткий, но в нём три места, где легко навредить, и все три
одинаковы у любой заставы:

    сломанный ввод         → пропустить, а не упасть
    своя ошибка            → пропустить, а не запретить всё
    не тот инструмент      → выйти мгновенно

Предмет этого модуля — **протокол хуков Claude Code**, а не «инструмент»: отсюда
папка `claude_/` (см. `code_rules.md` § 0.1 — папку называют по предмету). До этого
модуля обвязка стояла дважды — в `claude_gate_deploy` и `claude_gate_tool_exists_check` — и
расходилась уже на второй правке: одна застава проверяла имя инструмента как
`not in (None, 'Bash')`, другая как `not in ('Write', 'Edit')`, то есть по-разному
отвечала на пустое имя.

## ⚠⚠ Застава при своей поломке обязана пропускать

Сломанный хук, запрещающий всё, останавливает работу целиком и необъяснимо для
человека: команда не идёт, причины не видно. Непроверенная запись — беда меньшая, и
её видно. Поэтому исключение внутри проверки превращается в строку в `stderr` и код
`0`, а не в отказ.

⚠ По той же причине пропускается и ввод, который не разобрался: это не «подозрительный
вызов», а сломанная труба.

## Как этим пользуются

    def main() -> int:
        return claude_hook_run('Bash', _look)      # _look(данные) → замечание или ''

Функция проверки получает разобранный ввод и возвращает **текст замечания**; пустая
строка значит «пропустить». Ни кодов возврата, ни `stderr`, ни разбора JSON она не
касается — этим занят один этот модуль.

## Точка `Stop`: инструмента нет, а договор тот же

Застава на закрытии хода (`Stop`) приходит **без** `tool_name` и `tool_input`: у неё
нет вызова, который можно отменить, — отменяется сам конец хода. Проверять имя
инструмента не по чему, а разбор ввода, коды и «при своей поломке пропускать» — те же.
Для неё — `claude_hook_stop_run(look)`.

    def main() -> int:
        return claude_hook_stop_run(_look)

⚠ `Stop` зовётся **на каждый** конец хода, и замечание, данное ему, возвращается тем же
ходом снова — то есть застава зовёт саму себя. Claude Code помечает такие ходы
`stop_hook_active: true`, и заставка обязана читать этот флаг: без него она держит ход
до упора, а вид у этого выглядит как зависшая сессия.

## Точка `PostToolUse`: правка состоялась, отменять нечего

Инструмент уже отработал, поэтому двойка здесь **не отменяет** вызов — она отдаёт
`stderr` агенту замечанием к состоявшемуся. Обвязка та же, `claude_hook_run(tools, look)`;
меняется одна формулировка замечания: не «не пущу», а «не отменять, посмотреть». Так
стоит `claude_gate_code_review`.

## Харнесов два: Claude Code и DeepSeek Harness

Заставы написаны под хуки Claude Code, но код агент пишет и в DSH, где тот же договор
исполняет мост `@deepseek-ai/dsh-hooks-claude-code`. Он даёт два отличия, и оба
закрыты здесь, чтобы ни в одной заставе не было ветки «а вот в DSH ещё надо…»:

    tool_name           DSH зовёт инструмент строчными: `edit` вместо `Edit`
                        → сравнение имён — без учёта регистра (`claude_hook_is_tool`)
    transcript_path     мост не заполняет её вовсе, журнал сжат
                        → `claude_hook_journal_path` добирает файл по `session_id`,
                          а `claude_hook_transcript_rows` умеет читать `.jsonl.zstd`

## Чем ещё занят модуль

    claude_hook_journal_path(data)             # журнал сессии: `.jsonl` или `.jsonl.zstd`
    claude_hook_transcript_rows(path, limit)   # хвост журнала, не весь файл
    claude_hook_tool_calls(rows)               # вызовы инструментов + отметка их строки

Заставе на `Stop` правок хватает с их временем: отличить «это нарезали после того, как
я уже придралась» от «это было до» — то же различение порядка, что делает сама
застава. Разбор строк на вызовы лежит здесь, у разбора транскрипта, а не в каждой
заставе: второй копией этот разбор уже расходился (см. заголовок модуля).

Заставе на `Stop` смотреть нечего, кроме транскрипта: ни имени инструмента, ни доводов
ей не присылают. Транскрипты на живых машинах наработаны до **154 МБ**, поэтому
помощник читает только хвост — см. ⚠⚠ в его докстринге.
"""
import json
import subprocess
import sys
from pathlib import Path

# Коды возврата по договору Claude Code. ⚠ Не `1`: единица значит «сам хук сломался»,
# и вызов при ней проходит — то есть замечание до агента не дойдёт.
CLAUDE_HOOK_PASS = 0
CLAUDE_HOOK_BLOCK = 2

# Сколько байт с конца транскрипта читаем. ⚠ Потолок, а не «примерно на N строк»:
# строка с выводом инструмента весит мегабайты, поэтому окно меряем байтами.
CLAUDE_HOOK_TAIL_BYTES = 2 * 1024 * 1024

# Журнал сессии DeepSeek Harness: `<корень>/sessions/<проект>/session-<id>/session.v*.jsonl.zstd`.
CLAUDE_HOOK_DSH_SESSIONS = Path.home() / '.dsh' / 'sessions'
CLAUDE_HOOK_DSH_JOURNAL = 'session.v*.jsonl.zstd'
CLAUDE_HOOK_DSH_CALL = 'tool/call'       # запись DSH: зов инструмента, в `data.name`/`data.arguments`

# Секунд на распаковку журнала. Застава не обязана ждать вечно: не успела — молчим.
CLAUDE_HOOK_ZSTD_TIMEOUT = 20


def claude_hook_input() -> dict:
    """Разобранный ввод хука со stdin.

    Returns:
        dict: то, что прислал Claude Code — `tool_name`, `tool_input`, `cwd` и прочее.
        Пустой словарь — ввод не разобрался.

    ⚠⚠ Сломанный ввод отдаётся пустым словарём, а не исключением: застава стоит на
    **каждом** вызове инструмента, и упади она — работа встанет целиком.
    """
    try:
        return json.loads(sys.stdin.read() or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}


def claude_hook_is_tool(name, tools) -> bool:
    """То ли это имя инструмента — без учёта регистра и написания харнеса.

    Args:
        name: имя из ввода хука или из журнала сессии.
        tools: имена инструментов, на которые поставлена застава.

    ⚠ Регистр не значим намеренно: Claude Code зовёт инструмент `Edit`, DeepSeek
    Harness — `edit`, а застава одна на оба. Сравнивать в заставе — значит завести
    второй список имён на каждый харнес.

    ⚠ Пустое имя — никогда не наш случай: так выглядит ввод, который не разобрался.
    """
    got = str(name or '').lower()

    return bool(got) and got in {str(one).lower() for one in tools}


def claude_hook_wanted(data, tools) -> bool:
    """Про этот ли вызов застава.

    Args:
        data: разобранный ввод хука.
        tools: имена инструментов, на которые застава поставлена.

    ⚠ Пустое имя инструмента — не наш случай: так выглядит ввод, который не
    разобрался, а пропускать его должна не проверка, а обвязка.
    """
    return claude_hook_is_tool((data or {}).get('tool_name'), tools)


def claude_hook_journal_path(data) -> str:
    """Файл журнала этой сессии — из Claude Code или из DeepSeek Harness.

    Args:
        data: разобранный ввод хука — `transcript_path`, `session_id`.

    Returns:
        str: путь к журналу, который умеет читать `claude_hook_transcript_rows`;
        пусто — журнала нет, и заставе, которой мало одного вызова, надо молчать.

    Claude Code кладёт путь в `transcript_path`. Мост `dsh-hooks-claude-code`
    **не заполняет его вовсе** — сегмент сохранения DSH не отдаёт путей, а журнал
    сжат; поэтому для DSH журнал добирается по `session_id`. Без этого две заставы,
    которые смотрят на правки (`claude_gate_design_review_live`,
    `claude_gate_code_review`), в DSH ослепли бы.

    ⚠ Пусто отдаётся **молча**: DSH может писать журнал в другую сторону, а id
    сессии может оказаться чужим — это не поломка, а «сегодня без истории».
    """
    stated = str((data or {}).get('transcript_path') or '')

    if stated:
        return stated

    return _dsh_journal(str((data or {}).get('session_id') or ''))


def claude_hook_run(tools, look) -> int:
    """Прогнать заставу и вернуть код возврата для Claude Code.

    Args:
        tools: имя инструмента или список имён, на которые застава поставлена.
        look: проверка — получает разобранный ввод, возвращает текст замечания;
            пустая строка значит «пропустить».

    Returns:
        int: `CLAUDE_HOOK_PASS` или `CLAUDE_HOOK_BLOCK`.

    ⚠⚠ Своя ошибка проверки пропускает вызов — см. ⚠⚠ в докстринге модуля. Текст
    ошибки при этом печатается: молчащая сломанная застава хуже видимой.
    """
    names = (tools,) if isinstance(tools, str) else tuple(tools)
    data = claude_hook_input()

    if not claude_hook_wanted(data, names):
        return CLAUDE_HOOK_PASS

    try:
        said = look(data)
    except Exception as bad:      # noqa: BLE001 — см. ⚠⚠ выше
        print(f'застава сорвалась, вызов пропущен: {bad}', file=sys.stderr)

        return CLAUDE_HOOK_PASS

    if not said:
        return CLAUDE_HOOK_PASS

    print(said, file=sys.stderr)

    return CLAUDE_HOOK_BLOCK


def claude_hook_stop_run(look) -> int:
    """Прогнать заставу на закрытии хода и вернуть код возврата для Claude Code.

    Args:
        look: проверка — получает разобранный ввод, возвращает текст замечания;
            пустая строка значит «ход отпущен».

    Returns:
        int: `CLAUDE_HOOK_PASS` или `CLAUDE_HOOK_BLOCK` — при втором Claude Code не
            заканчивает ход, а отдаёт `stderr` агенту как указание, что доделать.

    От `claude_hook_run` отличается ровно одним: у `Stop` имени инструмента нет, и
    отменять нечего — под вопросом сам конец хода. Проверять имя не по чему, поэтому
    вся остальная обвязка (разбор, коды, «при своей поломке пропускать») общая.
    """
    data = claude_hook_input()

    if not data:
        return CLAUDE_HOOK_PASS

    try:
        said = look(data)
    except Exception as bad:      # noqa: BLE001 — см. ⚠⚠ выше
        print(f'застава сорвалась, ход отпущен: {bad}', file=sys.stderr)

        return CLAUDE_HOOK_PASS

    if not said:
        return CLAUDE_HOOK_PASS

    print(said, file=sys.stderr)

    return CLAUDE_HOOK_BLOCK


def claude_hook_transcript_rows(path, limit) -> list:
    """Последние записи транскрипта Claude, от старых к новым.

    Args:
        path: `transcript_path` из ввода хука — абсолютный путь к `.jsonl`.
        limit: сколько последних записей отдать; читаем шире, режем по этому числу.
            Ноль и меньше — пустой список, а не «все»: отрицательный срез в Python
            отдал бы весь хвост, и застава разобрала бы мегабайты вместо ничего.

    Returns:
        list: разобранные словари строк. Битая строка пропускается, целиком
            нечитаемый файл отдаёт пустой список — заставка при этом молчит.

    ⚠⚠ Читаем **хвост**, а не файл: живые транскрипты наработаны до 154 МБ, и
    прочитанный целиком, один такой встанет на десятки секунд на каждом конце хода.
    Всё, что нужно заставе, — чем кончался ход, а это последние десятки строк.

    ⚠ Журнал DSH сжат (`.jsonl.zstd`) — распаковка идёт наружу, `zstd -dc | tail -c`;
    сжатого целиком в память не берём. Пакета `zstd` в системе нет — пустой список,
    то есть застава молчит, а не падает.

    ⚠ Строка, с которой началось чтение, обрезана слева и не парсится — она
    отбрасывается молча, это не поломка файла. Короткий файл читается целиком,
    и первая строка его остаётся на месте.
    """
    chunk, cut = _tail_bytes(str(path))
    rows = []

    # Обрезанную слева строку отбрасываем, но только если встали в середину файла.
    for line in chunk.splitlines()[1:] if cut else chunk.splitlines():
        try:
            rows.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue

    tail = int(limit)

    return rows[-tail:] if tail > 0 else []


def claude_hook_tool_calls(rows) -> list:
    """Вызовы инструментов из записей транскрипта — имя, доводы, время строки.

    Args:
        rows: записи от `claude_hook_transcript_rows` (или готовые из теста).

    Returns:
        list: `[{'name', 'input', 'at'}]` в порядке транскрипта; `at` — `timestamp`
        строки строкой же (ISO, `2026-09-25T11:49:26.264Z`), пустая строка — времени
        в строке нет.

    Отметка здесь затем, что заставе на `Stop` мало «файл правили»: ей нужно отсечь
    правки, о которых она уже придралась, от нарезанных после. Порядок записей —
    порядок хода, так что сравнить две отметки достаточно строкой.

    ⚠ Строка без `message`/`content` и запись не-словарь пропускаются молча:
    транскрипт полон служебных строк (`file-history-*`, `system`), зова инструмента
    в них нет.

    ⚠ Форма записи зависит от харнеса: у Claude Code зов живёт в
    `message.content[]` с `type: tool_use`, у DSH — отдельной строкой `tool/call`
    с `data.name` и `data.arguments` (**строкой** JSON, а не словарём). Разбор
    двух форм здесь, а не в каждой заставе: наружу выходит один список.
    """
    out = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        if row.get('type') == CLAUDE_HOOK_DSH_CALL:
            body = row.get('data') or {}

            out.append({'name': str(body.get('name') or ''),
                        'input': _row_args(body.get('arguments')),
                        'at': str(row.get('time') or '')})

            continue

        stamp = str(row.get('timestamp') or '')
        content = (row.get('message') or {}).get('content') or []

        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get('type') == 'tool_use':
                out.append({'name': str(part.get('name') or ''),
                            'input': part.get('input') or {}, 'at': stamp})

    return out


# --- Приватные: ниже всех публичных (code_rules.md, §3) -----------------------

def _tail_bytes(path):
    """Хвост журнала байтами и признак, что первая строка обрезана слева."""
    if path.endswith('.zstd'):
        return _tail_zstd(path)

    try:
        with open(path, 'rb') as handle:
            handle.seek(0, 2)
            cut = max(0, handle.tell() - CLAUDE_HOOK_TAIL_BYTES)
            handle.seek(cut)

            return handle.read().decode('utf-8', 'replace'), bool(cut)
    except (OSError, UnicodeError):
        return '', False


def _tail_zstd(path):
    """Хвост сжатого журнала DSH — распаковкой в pipe, целиком в память не берём."""
    try:
        got = subprocess.run(
            ['sh', '-c', 'zstd -dc "$1" 2>/dev/null | tail -c "$2"', 'claude_hook',
             path, str(CLAUDE_HOOK_TAIL_BYTES)],
            capture_output=True, timeout=CLAUDE_HOOK_ZSTD_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):     # zstd нет, файл недоступен, timeout
        return '', False

    chunk = (got.stdout or b'').decode('utf-8', 'replace')

    return chunk, len(chunk.encode('utf-8')) >= CLAUDE_HOOK_TAIL_BYTES


def _dsh_journal(session_id) -> str:
    """Файл журнала DSH по id сессии; пусто — файла нет.

    ⚠ `session_id` из ввода хука — это **уже** имя каталога: DSH называет сессию
    `session-<uuid>` и тем же id подписывает первую строку своего журнала. Ищем обеими
    формами — с префиксом и без: отдавал ли мост id голым uuid, зависит от сборки, а на
    одной из них заставка ослепла бы молча.
    """
    key = str(session_id or '').removeprefix('session-')

    if not key or set(key.lower()) - set('0123456789abcdef-'):
        return ''                                    # id с спецсимволами сломал бы glob

    seats = []

    for name in (key, f'session-{key}'):
        try:
            seats += list(CLAUDE_HOOK_DSH_SESSIONS.glob(f'*/{name}'))
        except OSError:                              # homes с правами на чтение
            continue

    newest = ''
    stamp = -1.0

    for seat in seats:
        for found in seat.glob(CLAUDE_HOOK_DSH_JOURNAL):
            try:
                seen = found.stat().st_mtime
            except OSError:
                continue

            if seen > stamp:
                newest, stamp = str(found), seen

    return newest


def _row_args(arguments) -> dict:
    """Доводы зова из журнала DSH: там JSON строкой, а не разобранным словарём."""
    if isinstance(arguments, dict):
        return arguments

    try:
        return json.loads(str(arguments or '{}'))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}

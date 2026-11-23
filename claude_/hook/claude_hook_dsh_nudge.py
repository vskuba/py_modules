"""
Застава «отдай разведку субагенту»: серия чтений и ни одного `dsh-board`.

Хук Claude Code на `PostToolUse` (`Read|Grep|Glob|Bash`). Правило «объёмную разведку —
субагентам DeepSeek» записано в `~/.claude/CLAUDE.md`, но на память оно не работает:
агент, занятый задачей, читает файл за файлом сам.

Считается **серия чтений** — с самой свежей из трёх засечек:

- вызова `dsh-board` — разведку уже отдали, счёт с нуля;
- сообщения человека — начался новый ход;
- строки `Сам:` в ответе агента — он объяснил, почему делает сам.

Напоминание одно на серию и только в режиме FREE: в HIGH на пути встаёт
`claude_gate_dsh_delegate` (она отменяет вызов, напоминание там лишнее), в OFF
субагентов не зовут вовсе.

Классификация «чтения» и подсчёт серии — публичные (`..._is_read`, `..._read_series`) и
зовутся обеими заставы: второй копии этих правил в репозитории быть не должно. Первый же
`cd X && Q() { …mysql_query… }; Q` прошёл бы мимо регулярки, ловившей **только начало
строки**, — так и было: один `dsh-board` в ходе глушил напоминание до его конца, а
`git fetch && ls` не считалось чтением вовсе.

    echo '{"transcript_path":"…/x.jsonl","session_id":"s"}' \\
      | python3 claude_/hook/claude_hook_dsh_nudge.py
"""
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from claude_.hook.claude_hook import claude_hook_input, claude_hook_is_tool, \
    claude_hook_journal_path, claude_hook_tool_calls, claude_hook_transcript_rows  # noqa: E402
from dsh_.dsh_mode import DSH_MODE_FREE, dsh_mode_get  # noqa: E402

# Сколько чтений в серии — повод напомнить.
CLAUDE_HOOK_DSH_NUDGE_READS = 8
# Сколько последних записей транскрипта смотреть: серия длиннее — уже напоминали бы.
CLAUDE_HOOK_DSH_NUDGE_ROWS = 400
# Где помнить, на какой серии этой сессии уже напоминали.
CLAUDE_HOOK_DSH_NUDGE_STATE = os.path.expanduser('~/.claude/state/dsh-nudge.json')

# Инструменты, которые читают всегда.
CLAUDE_HOOK_DSH_NUDGE_READ_TOOLS = ('Read', 'Grep', 'Glob')
# Команды, которые читают: разведка, а не работа. Форма «слово + флаг» — `sed -n` (а `sed -i` — правка).
CLAUDE_HOOK_DSH_NUDGE_READ_COMMANDS = ('cat', 'grep', 'rg', 'ugrep', 'sed -n', 'head', 'tail', 'find',
                                       'ls', 'wc', 'awk', 'jq', 'tree', 'stat', 'file', 'diff')
# `git` читает только этими подкомандами: `git commit` — это уже работа.
CLAUDE_HOOK_DSH_NUDGE_GIT_READ = ('log', 'show', 'diff', 'grep', 'blame', 'status', 'ls-files')
# Глобальные флаги git со значением: подкоманда идёт после него, а не вместо неё.
CLAUDE_HOOK_DSH_NUDGE_GIT_VALUE_FLAGS = ('-C', '-c', '--git-dir', '--work-tree', '--namespace')
# Запрос к базе и к журналу — тоже чтение, хоть командой чтения и не выглядит.
CLAUDE_HOOK_DSH_NUDGE_READ_MARKS = ('mysql_query', 'opensearch_')
# Делегирование: зов доски субагентов. Чтением не считается, серию обнуляет.
CLAUDE_HOOK_DSH_NUDGE_DELEGATE = ('dsh-board', 'dsh_board')
# `curl` читает, пока в нём нет флага записи.
CLAUDE_HOOK_DSH_NUDGE_CURL_WRITE_RE = re.compile(r'(?:^|\s)(?:-X\s*(?:POST|PUT|DELETE)|-d\b|--data)', re.I)
# Куски составной команды.
CLAUDE_HOOK_DSH_NUDGE_SPLIT_RE = re.compile(r'&&|\|\||[;|\n]')
# Ведущие `cd <путь>` и присваивания `VAR=…` — обстановка команды, а не её действие.
CLAUDE_HOOK_DSH_NUDGE_LEADING_RE = re.compile(
    r'^(?:cd\s+(?:"[^"]*"|\'[^\']*\'|[^\s]+)|[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|\'[^\']*\'|[^\s]*))\s*')
# Строка, с которой серия начинается заново: обоснование «делаю сам», сказанное человеку.
CLAUDE_HOOK_DSH_NUDGE_SOLO = 'Сам:'

CLAUDE_HOOK_DSH_NUDGE_TEXT = (
    'В этой сессии подряд {reads} чтений и ни одной задачи субагенту. Разведку по коду '
    '(где и как устроено, сверка, поиск) отдавай агенту DeepSeek: `dsh-board new "<проект>" '
    '"<имя>" --ask "<что найти>" --wait --out <файл>` — он прочитает за тебя, тебе придёт '
    'итог. Себе оставь решения и правку. Напоминание одно на серию чтений.'
)


def claude_hook_dsh_nudge_is_read(call: dict) -> bool:
    """Чтение ли этот вызов — разведка, а не работа.

    Args:
        call: вызов из транскрипта (`claude_hook_tool_calls`) или пара `{'name', 'input'}`
            из ввода хука — застава судит текущий вызов **до** того, как он попадёт в журнал.

    `Read`/`Grep`/`Glob` читают всегда. `Bash` читает, если **любой** кусок команды —
    команда чтения (`cat`, `grep`, `git log`, `jq`, …), запрос в базу (`mysql_query`) или
    в журнал (`opensearch_`), либо `curl` без флага записи. `dsh-board` — не чтение, а
    делегирование.

    ⚠ Кусок — не начало строки: `cd X && Q() { …mysql_query… }; Q`, `git fetch && ls` и
    `cat a | jq` начинаются не с чтения, разведкой при этом быть не перестают.
    """
    name = str((call or {}).get('name') or '')

    if claude_hook_is_tool(name, CLAUDE_HOOK_DSH_NUDGE_READ_TOOLS):
        return True

    if not claude_hook_is_tool(name, ('Bash',)):
        return False

    command = _command_of(call)

    if _is_delegate(command):
        return False

    # Запрос в базу или в журнал — чтение, где бы имя ни стояло: в теле функции, в `$(…)`.
    if any(mark in command for mark in CLAUDE_HOOK_DSH_NUDGE_READ_MARKS):
        return True

    return any(_segment_is_read(one) for one in _segments(command))


def claude_hook_dsh_nudge_read_series(rows: list) -> dict:
    """Серия чтений: сколько их подряд и с какой записи считаются.

    Args:
        rows: записи транскрипта от `claude_hook_transcript_rows` (или готовые из теста).

    Returns:
        dict: `{'reads': int, 'start': str}` — число чтений с начала серии и маркер её
        начала (`uuid`, а нет его — время записи); пустой маркер — засечки в этом хвосте
        нет и считён весь хвост.

    Начало серии — самая свежая из трёх засечек: вызов `dsh-board` (разведку уже отдали),
    сообщение человека (новый ход) или строка `Сам:` в ответе агента (объяснил, почему
    делает сам). Без этого один `dsh-board` в начале хода глушил счёт на весь ход, а
    честное «Сам: ищу одну строку» продолжало копить придирки.

    ⚠ Маркер начала — признак **смены серии** для памяти заставы: счётчик сверяют с ним,
    иначе отмена, данная в прошлой серии, считалась бы данной в этой.
    """
    start = _series_start(rows)
    calls = claude_hook_tool_calls(rows[start + 1:])
    reads = sum(1 for call in calls if claude_hook_dsh_nudge_is_read(call))

    return {'reads': reads, 'start': _row_key(rows[start]) if 0 <= start < len(rows) else ''}


def claude_hook_dsh_nudge_check(rows: list) -> int:
    """Сколько чтений в серии, если пора напомнить; иначе 0."""
    reads = claude_hook_dsh_nudge_read_series(rows)['reads']

    return reads if reads >= CLAUDE_HOOK_DSH_NUDGE_READS else 0


def main() -> int:
    """Хук `PostToolUse`: мягкое напоминание в FREE, молчание в HIGH (там застава) и в OFF."""
    try:
        return _run(claude_hook_input())
    except Exception as bad:                       # noqa: BLE001 — напоминание не держит ход
        print(f'напоминание сорвалось, ход идёт: {bad}', file=sys.stderr)

        return 0


# --- Приватные: ниже всех публичных (code_rules.md, §3) -----------------------

def _run(data: dict) -> int:
    """Заметить серию чтений и, если на ней ещё не напоминали, добавить контекст."""
    session = str(data.get('session_id') or '')

    if dsh_mode_get(session) != DSH_MODE_FREE:
        return 0                                   # HIGH — там застава, OFF — субагентов нет

    path = claude_hook_journal_path(data)

    if not path:
        return 0

    series = claude_hook_dsh_nudge_read_series(
        claude_hook_transcript_rows(path, CLAUDE_HOOK_DSH_NUDGE_ROWS))

    if series['reads'] < CLAUDE_HOOK_DSH_NUDGE_READS:
        return 0

    state = _state_read()

    if state.get(session) == series['start']:
        return 0                                   # на этой серии уже напоминали

    state[session] = series['start']
    _state_write(state)
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                            'additionalContext': CLAUDE_HOOK_DSH_NUDGE_TEXT.format(
                                                reads=series['reads'])}}, ensure_ascii=False))

    return 0


def _command_of(call: dict) -> str:
    """Команда Bash-вызова — из доводов Claude Code и из `arguments` журнала DSH."""
    return str(((call or {}).get('input') or {}).get('command') or '')


def _segments(command: str) -> list:
    """Куски составной команды: `&&`, `||`, `;`, `|` и перевод строки — граница куска."""
    return [one for one in CLAUDE_HOOK_DSH_NUDGE_SPLIT_RE.split(str(command or '')) if one.strip()]


def _strip_leading(segment: str) -> str:
    """Кусок без ведущих `cd <путь>` и присваиваний: `cd /x && grep` — это grep, а не cd."""
    line = str(segment or '').strip()

    for _ in range(5):
        cut = CLAUDE_HOOK_DSH_NUDGE_LEADING_RE.match(line)

        if not cut or cut.end() > len(line):
            break

        line = line[cut.end():].strip()

    return line


def _segment_is_read(segment: str) -> bool:
    """Читает ли этот кусок команды."""
    line = _strip_leading(segment)
    words = line.split()

    if not words:
        return False

    if words[0].rstrip(':').endswith('curl'):
        return not CLAUDE_HOOK_DSH_NUDGE_CURL_WRITE_RE.search(line)

    if any(words[:len(one.split())] == one.split() for one in CLAUDE_HOOK_DSH_NUDGE_READ_COMMANDS):
        return True

    return words[0] == 'git' and _git_subcommand(words) in CLAUDE_HOOK_DSH_NUDGE_GIT_READ


def _git_subcommand(words: list) -> str:
    """Подкоманда git — первый значимый знак после глобальных флагов (`-C`, `-c`, …)."""
    skip = False

    for word in words[1:]:
        if skip:
            skip = False
            continue

        if word in CLAUDE_HOOK_DSH_NUDGE_GIT_VALUE_FLAGS:
            skip = True
            continue

        if word.startswith('-') or word.startswith('='):
            continue

        return word

    return ''


def _is_delegate(command: str) -> bool:
    """Команда зовёт доску субагентов: это делегирование, а не чтение."""
    return any(_segments_delegate(one) for one in _segments(command))


def _segments_delegate(segment: str) -> bool:
    """Этот кусок — зов `dsh-board` (или `python -m dsh_.dsh_board_cli`), а не его упоминание."""
    words = _strip_leading(segment).split()

    if not words:
        return False

    head = words[0].rsplit('/', 1)[-1]

    if any(mark in head for mark in CLAUDE_HOOK_DSH_NUDGE_DELEGATE):
        return True

    return len(words) > 2 and words[0].startswith('python') and words[1] == '-m' \
        and 'dsh_board' in words[2]


def _series_start(rows: list) -> int:
    """Номер записи, с которой считается серия; -1 — засечки в этом хвосте нет."""
    start = _last_prompt_index(rows)

    for index, row in enumerate(rows):
        if index <= start:
            continue

        if _row_is_solo(row) or any(_is_delegate(_command_of(call)) for call in _row_calls(row)):
            start = index

    return start


def _last_prompt_index(rows: list) -> int:
    """Номер записи с последним сообщением человека; -1 — не нашлось в хвосте."""
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]

        if not isinstance(row, dict):
            continue

        if row.get('type') == 'user/message':
            if ((row.get('data') or {}).get('source') or {}).get('kind') == 'user':
                return index
            continue

        if row.get('type') != 'user':
            continue

        content = (row.get('message') or {}).get('content')

        if isinstance(content, str):
            return index

        if isinstance(content, list) and not any(isinstance(part, dict) and part.get('type') == 'tool_result'
                                                 for part in content):
            return index

    return -1


def _row_is_solo(row) -> bool:
    """Ответ агента с обоснованием «Сам: …» — с этой строки серия начинается заново."""
    if not isinstance(row, dict) or _row_role(row) != 'assistant':
        return False

    return any(one.lstrip().startswith(CLAUDE_HOOK_DSH_NUDGE_SOLO) for one in _row_text(row).splitlines())


def _row_role(row: dict) -> str:
    """Кто говорит: `assistant`, `user` — по форме Claude Code и по форме журнала DSH."""
    kind = str(row.get('type') or '')

    if kind in ('assistant', 'assistant/message'):
        return 'assistant'

    if kind in ('user', 'user/message'):
        return 'user'

    return str(((row.get('message') or {}).get('role')) or '')


def _row_text(row: dict) -> str:
    """Весь текст записи — реплики, а не выводов инструментов.

    ⚠ В `arguments` зовов не заглядываем: иначе `Сам:` оказалась бы в тексте написанного
    файла или в аргументе `grep`, и серия обнулилась бы на чужой строке.
    """
    body = row.get('message')

    if not isinstance(body, dict):
        body = (row.get('data') or {}).get('message') if isinstance(row.get('data'), dict) else {}

    out: list = []
    _collect_text(body, out)

    return '\n'.join(out)


def _collect_text(node, out: list) -> None:
    """Текст из разметки реплики: строки под ключом `text` и под `content`/`parts`."""
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, (list, tuple)):
        for one in node:
            _collect_text(one, out)
    elif isinstance(node, dict):
        for key in ('text', 'content', 'parts'):
            if key in node:
                _collect_text(node[key], out)


def _row_calls(row) -> list:
    """Зовы инструментов этой записи — общим разборчиком, по одной строке за раз."""
    return claude_hook_tool_calls([row]) if isinstance(row, dict) else []


def _row_key(row) -> str:
    """Маркер записи: `uuid`, иначе время, иначе подпись содержимого.

    ⚠ Номером строки маркер быть не может: хвост транскрипта сдвигается на каждую новую
    запись, и серия «начиналась бы» на новом месте каждый вызов.
    """
    if not isinstance(row, dict):
        return ''

    for key in ('uuid', 'timestamp', 'time'):
        if row.get(key):
            return str(row[key])

    try:
        raw = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ''

    return 'h' + hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:16]


def _state_read() -> dict:
    try:
        with open(CLAUDE_HOOK_DSH_NUDGE_STATE, encoding='utf-8') as handle:
            got = json.load(handle)
    except (OSError, ValueError):
        return {}

    return got if isinstance(got, dict) else {}


def _state_write(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CLAUDE_HOOK_DSH_NUDGE_STATE), exist_ok=True)
        with open(CLAUDE_HOOK_DSH_NUDGE_STATE, 'w', encoding='utf-8') as handle:
            json.dump(dict(list(state.items())[-50:]), handle)
    except OSError:
        pass


if __name__ == '__main__':
    sys.exit(main())

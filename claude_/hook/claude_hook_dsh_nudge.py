"""
Застава «отдай разведку субагенту»: много чтений за ход и ни одного `dsh-board`.

Хук Claude Code на `PostToolUse` (`Read|Grep|Glob|Bash`). Правило «объёмную разведку —
субагентам DeepSeek» записано в `~/.claude/CLAUDE.md`, но на память оно не работает:
агент, занятый задачей, читает файл за файлом сам. Застава считает чтения с последнего
сообщения человека и, когда их набралось `CLAUDE_HOOK_DSH_NUDGE_READS`, а `dsh-board`
в этом ходе не звался, **один раз за ход** добавляет в контекст напоминание.
Ничего не блокирует: отдавать ли — решает модель.

    echo '{"transcript_path":"…/x.jsonl","session_id":"s"}' \\
      | python3 claude_/hook/claude_hook_dsh_nudge.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from claude_.hook.claude_hook import claude_hook_input, claude_hook_tool_calls, \
    claude_hook_transcript_rows  # noqa: E402

# Сколько чтений за ход — повод напомнить.
CLAUDE_HOOK_DSH_NUDGE_READS = 8
# Сколько последних записей транскрипта смотреть: ход длиннее — уже напоминали бы.
CLAUDE_HOOK_DSH_NUDGE_ROWS = 400
# Где помнить, на каком сообщении человека уже напомнили.
CLAUDE_HOOK_DSH_NUDGE_STATE = os.path.expanduser('~/.claude/state/dsh-nudge.json')
# Команды Bash, которые только читают: разведка, а не работа.
CLAUDE_HOOK_DSH_NUDGE_BASH_READ = re.compile(r'^\s*(cd \S+ && )?(cat|grep|rg|sed -n|head|tail|find|ls|wc|awk)\b')

CLAUDE_HOOK_DSH_NUDGE_TEXT = (
    'В этом ходе уже {reads} чтений файлов и ни одной задачи субагенту. Разведку по коду '
    '(где и как устроено, сверка, поиск) отдавай агенту DeepSeek: `dsh-board new "<проект>" '
    '"<имя>" --ask "<что найти>" --wait --out <файл>` — он прочитает за тебя, тебе придёт '
    'итог. Себе оставь решения и правку. Напоминание одно на ход.'
)


def claude_hook_dsh_nudge_check(rows: list) -> int:
    """Сколько чтений с последнего сообщения человека, если пора напомнить; иначе 0."""
    start = _last_prompt_index(rows)
    calls = claude_hook_tool_calls(rows[start + 1:])
    if any('dsh-board' in str((call['input'] or {}).get('command', '')) for call in calls):
        return 0
    reads = sum(1 for call in calls if _is_read(call))
    return reads if reads >= CLAUDE_HOOK_DSH_NUDGE_READS else 0


def main() -> int:
    data = claude_hook_input()
    path = data.get('transcript_path') or ''
    if not path:
        return 0
    rows = claude_hook_transcript_rows(path, CLAUDE_HOOK_DSH_NUDGE_ROWS)
    reads = claude_hook_dsh_nudge_check(rows)
    if not reads:
        return 0
    prompt = rows[_last_prompt_index(rows)].get('uuid', '') if rows else ''
    session = data.get('session_id') or ''
    state = _state_read()
    if state.get(session) == prompt:
        return 0                     # на этом сообщении уже напоминали
    state[session] = prompt
    _state_write(state)
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                             'additionalContext': CLAUDE_HOOK_DSH_NUDGE_TEXT.format(reads=reads)}},
                     ensure_ascii=False))
    return 0


# --- Приватные: ниже всех публичных (code_rules.md, §3) -----------------------

def _last_prompt_index(rows: list) -> int:
    """Номер записи с последним сообщением человека; -1 — не нашлось в хвосте."""
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.get('type') != 'user':
            continue
        content = (row.get('message') or {}).get('content')
        if isinstance(content, str):
            return index
        if isinstance(content, list) and not any(isinstance(part, dict) and part.get('type') == 'tool_result'
                                                 for part in content):
            return index
    return -1


def _is_read(call: dict) -> bool:
    name = call['name']
    if name in ('Read', 'Grep', 'Glob'):
        return True
    return name == 'Bash' and bool(CLAUDE_HOOK_DSH_NUDGE_BASH_READ.match(str((call['input'] or {}).get('command', ''))))


def _state_read() -> dict:
    try:
        with open(CLAUDE_HOOK_DSH_NUDGE_STATE, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _state_write(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CLAUDE_HOOK_DSH_NUDGE_STATE), exist_ok=True)
        with open(CLAUDE_HOOK_DSH_NUDGE_STATE, 'w', encoding='utf-8') as handle:
            json.dump(dict(list(state.items())[-50:]), handle)
    except OSError:
        pass


if __name__ == '__main__':
    sys.exit(main())

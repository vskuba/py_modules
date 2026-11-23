"""
Застава «отдай разведку субагенту», которая встаёт на пути: серия чтений — и вызов отменён.

Работает хуком Claude Code на `PreToolUse` (`Read|Grep|Glob|Bash`), общим для всех
проектов. **Только в режиме HIGH** (`dsh_/dsh_mode.py`): там человек уже сказал «максимум
работы субагентам DeepSeek», и напоминание, которое можно прочитать мимо, — не инструмент.
На пятое чтение подряд без `dsh-board` застава **отменяет вызов** и отдаёт готовую команду;
в FREE молчит (там работает мягкое напоминание `claude_hook_dsh_nudge`), в OFF — тем более.

## ⚠⚠ Отменяет один раз на серию, а не держит

    1…4-е чтение    → пропуск
    5-е чтение      → отмена + готовая команда
    6-е чтение      → пропуск            ← повтор того же вызова проходит
    … следующие 5   → до новой отмены

Застава, отменяющая каждое чтение, — это застава, которую снимут в тот же день: чтение
нужно, чтобы работать. Её задача — не запретить чтение, а не дать разведке **незаметно
стать работой**. Классификация чтений и подсчёт серии — у `claude_hook_dsh_nudge`:
правило живёт в одном месте, иначе застава и напоминание начнут спорить.

## Как пройти, не отдавая

Обоснование, а не обход: строка, начинающаяся с `Сам:`, в ответе агента после последнего
`dsh-board` переносит начало серии на это место — счётчик начинается с нуля, отмены нет,
пока с тех пор снова не набралось `CLAUDE_GATE_DSH_DELEGATE_READS` чтений. Строка
читается из **текста реплики**, а не из аргументов инструментов: иначе `Сам:` нашлось бы
в тексте записываемого файла или в доводе `grep`.

## Чего застава не делает

- **не отменяет в FREE и в OFF** — режим решает человек, застава ему не перечит; в HIGH,
  наоборот, не ограничивается напоминанием;
- **не считает чтением `dsh-board`** — это делегирование, оно серию и обнуляет;
- **не смотрит на содержимое правок, ссылок и поиска** — судит только по имени инструмента
  и команде Bash;
- **не заводит память о «прощённом»**: отмена стоит в серии на своём месте, а не на сутки
  выключает сессию.

⚠ Память — **позиция в серии**, а не бюджет придирок, поэтому она не в `claude_gate`
(та память протухает сутки, а протухшая позиция отменила бы вызов заново на том же месте):
`~/.claude/state/dsh-delegate.json` по `session_id`, как у напоминания. Сменилась серия
(новый ход, `dsh-board` или `Сам:`) — позиция не действует.

⚠ При своей поломке — сломанный журнал, битая память, незнакомая форма записей — застава
**пропускает** вызов (`claude_hook.md`): отменить работу без объяснения дороже, чем
пропустить лишнее чтение.

    echo '{"tool_name":"Read","tool_input":{"file_path":"a.py"},"session_id":"s"}' \\
      | python3 claude_/hook/gate/claude_gate_dsh_delegate.py
"""
import json
import sys
from pathlib import Path

# ⚠ Загрузка через `spec_from_file_location` (так делает сюита) не кладёт корень
# слоя в путь поиска сама, а запуск файла по пути — кладёт через sys.path[0].
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from claude_.hook.claude_hook import claude_hook_input, claude_hook_journal_path, \
    claude_hook_transcript_rows, claude_hook_wanted  # noqa: E402
from claude_.hook.claude_hook_dsh_nudge import claude_hook_dsh_nudge_is_read, \
    claude_hook_dsh_nudge_read_series, CLAUDE_HOOK_DSH_NUDGE_ROWS  # noqa: E402
from claude_.hook.gate.claude_gate import claude_gate_off  # noqa: E402
from dsh_.dsh_mode import DSH_MODE_HIGH, dsh_mode_get  # noqa: E402

# Сколько чтений подряд — до отмены. Порог ниже четырёх означал бы заставу, встающую на
# пути у поиска одной строки: её бы снимали, а правило осталось бы невыполненным.
CLAUDE_GATE_DSH_DELEGATE_READS = 5

# Где помнить, на каком чтении этой сессии уже отменяли. ⚠ Не в проекте: застава общая.
CLAUDE_GATE_DSH_DELEGATE_STATE = Path.home() / '.claude' / 'state' / 'dsh-delegate.json'

# Инструменты, на которые поставлена застава.
CLAUDE_GATE_DSH_DELEGATE_TOOLS = ('Read', 'Grep', 'Glob', 'Bash')

# Переменная окружения, снимающая заставу целиком. ⚠ Имя своё и длинное: клапан —
# договор с человеком (claude_gate.md, грабля 3).
CLAUDE_GATE_DSH_DELEGATE_PASS = 'CLAUDE_GATE_DSH_DELEGATE_PASS'

CLAUDE_GATE_DSH_DELEGATE_TEXT = (
    'Чтение отменено заставой — на один раз. Подряд {reads} чтений (Read/Grep/Glob, '
    'cat/grep/git log/jq, запросы в базу) и ни одного вызова dsh-board, а режим этой '
    'сессии HIGH: объёмную разведку ведёт субагент DeepSeek.\n'
    '  dsh-board new "<проект>" "<имя>" --ask "<что найти>" --wait --out <файл>\n'
    '  dsh-board fanout tasks.json --out-dir <каталог>   # независимые куски — разом\n'
    'Следующий такой же вызов пройдёт: счётчик серии обнулён, до новой отмены ещё '
    '{quota} чтений.\n'
    'Делаешь сам — скажи человеку строку, начинающуюся с «Сам:», и объясни почему '
    '(«Сам: ищу одну строку, описать задачу дороже»); с этой строки серия начнётся заново.'
)


def claude_gate_dsh_delegate_check(data: dict, rows: list = None, mode: str = '',
                                   state_path=None) -> dict:
    """Отменять ли этот вызов и почему.

    Args:
        data: ввод хука `PreToolUse` (`tool_name`, `tool_input`, `session_id`, …).
        rows: записи транскрипта; нет их — читаются по `transcript_path` (в тесте — готовые).
        mode: режим сессии; пусто — берётся у `dsh_mode_get` (в тесте — явный).
        state_path: где память о состоявшейся отмене; пусто — `CLAUDE_GATE_DSH_DELEGATE_STATE`.

    Returns:
        dict: `{'block', 'reads', 'why'}` — `block` true отменять, `reads` сколько их
        подряд вышло с этим вызовом, `why` что заставило или почему молчит.

    ⚠ Текущий вызов в журнал ещё не попал — `PreToolUse` зовётся до него, — поэтому его
    плюсуют к счётчику, а не ищут в `rows`.
    """
    session = str((data or {}).get('session_id') or '')
    memory = Path(state_path or CLAUDE_GATE_DSH_DELEGATE_STATE)

    if claude_gate_off(CLAUDE_GATE_DSH_DELEGATE_PASS):
        return {'block': False, 'reads': 0, 'why': f'снята через {CLAUDE_GATE_DSH_DELEGATE_PASS}'}

    gone = mode or dsh_mode_get(session)

    if gone != DSH_MODE_HIGH:
        return {'block': False, 'reads': 0, 'why': f'режим {gone}: заставы нет'}

    if not claude_hook_dsh_nudge_is_read(_call(data)):
        return {'block': False, 'reads': 0, 'why': 'это не чтение'}

    if rows is None:
        rows = claude_hook_transcript_rows(claude_hook_journal_path(data) or '', CLAUDE_HOOK_DSH_NUDGE_ROWS)

    series = claude_hook_dsh_nudge_read_series(rows)
    reads = int(series['reads']) + 1
    noted = _state_read(memory).get(session)
    spent = 0

    if isinstance(noted, dict) and str(noted.get('series') or '') == str(series['start']):
        spent = _as_int(noted.get('spent'))         # позиция прошлой отмены — та же серия

    if reads - spent < CLAUDE_GATE_DSH_DELEGATE_READS:
        return {'block': False, 'reads': reads, 'why': 'серия короче порога'}

    _state_write(memory, session, {'series': series['start'], 'spent': reads})

    return {'block': True, 'reads': reads, 'why': 'чтений подряд больше порога, dsh-board не звался'}


def claude_gate_dsh_delegate_format(verdict: dict) -> str:
    """Причина отмены для `permissionDecisionReason`; пусто — пропустить."""
    if not (verdict or {}).get('block'):
        return ''

    return CLAUDE_GATE_DSH_DELEGATE_TEXT.format(reads=verdict.get('reads'),
                                                quota=CLAUDE_GATE_DSH_DELEGATE_READS)


def main() -> int:
    """Хук `PreToolUse`: печатью `permissionDecision: deny` отменяет вызов; молчание пропускает.

    ⚠ Код возврата всегда 0: двойка показала бы текст как ошибку хука, а здесь отказ —
    решение заставы, оформленное по договору `PreToolUse`.
    """
    data = claude_hook_input()

    if not claude_hook_wanted(data, CLAUDE_GATE_DSH_DELEGATE_TOOLS):
        return 0

    try:
        said = claude_gate_dsh_delegate_format(claude_gate_dsh_delegate_check(data))
    except Exception as bad:                        # noqa: BLE001 — застава не держит чужую работу
        print(f'застава сорвалась, вызов пропущен: {bad}', file=sys.stderr)

        return 0

    if not said:
        return 0

    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                            'permissionDecision': 'deny',
                                            'permissionDecisionReason': said}}, ensure_ascii=False))

    return 0


# --- Приватные: ниже всех публичных (code_rules.md, §3) -----------------------

def _call(data: dict) -> dict:
    """Текущий вызов в форме, в которой `…_is_read` видит вызовы из транскрипта."""
    return {'name': (data or {}).get('tool_name'), 'input': (data or {}).get('tool_input') or {}}


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _state_read(path: Path) -> dict:
    """Память сессий; нет файла, битый файл, чужая форма — пусто, а не исключение."""
    try:
        got = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

    return got if isinstance(got, dict) else {}


def _state_write(path: Path, session: str, note: dict) -> None:
    """Запомнить позицию отмены; не суметь — значит отменить лишний раз, а это удобнее падения."""
    try:
        state = _state_read(path)
        state[session] = note

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(dict(list(state.items())[-50:]), ensure_ascii=False),
                              encoding='utf-8')
    except (OSError, TypeError, ValueError):
        pass


if __name__ == '__main__':
    sys.exit(main())

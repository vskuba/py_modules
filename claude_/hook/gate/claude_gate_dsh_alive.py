"""
Застава «жди живого субагента»: пока карточка сессии работает, читать код нельзя.

Правило строже заставы делегирования: отдал работу субагенту DSH — жди его ответа. Пока
хотя бы одна карточка сессии работает, сессия сама не читает код (`Read`/`Grep`/`Glob`,
`cat`/`grep`/`jq`) — ни в HIGH, ни в FREE, ни в OFF; отказывает **каждый** вызов, обхода
«Сам:» нет. Карточка без ответа и без признаков жизни мёртвая — держать из-за неё нельзя;
когда у всех карточек ответы пришли, но не забраны, — отмена с текстом «забери его».

Признаки в одном месте каждый: метка и сколько ждут — `dsh_wait`, «ответ есть» —
`dsh_wait_answered` (тот же, что у фонового ждуна), жива ли карточка — занятый мотор
`dsh_board_motors_busy` или `running` у `dsh_board_sessions`. Не трогает застава вызовы
`dsh-board` (это делегирование, не чтение — решает `claude_hook_dsh_nudge_is_read`),
правки и чтение своих файлов: ответов в `/tmp`, памяти в `~/.claude`, меток в `~/.dsh`.

```bash
echo '{"session_id":"…","tool_name":"Read","tool_input":{"file_path":"x.py"}}' \
  | python3 claude_/hook/gate/claude_gate_dsh_alive.py
```

⚠⚠ Своя поломка (нет хоста, битые метки) пропускает вызов: застава, встав на каждом
чтении, стеной быть не должна (`claude_hook.md`). Недоступный хост — тоже пропуск:
доказать, что субагент жив, нельзя, а владелицу меток держат ждун и застава на `Stop`.
⚠ Отказывает каждый вызов и ничего не помнит: в отличие от заставы делегирования чтение
здесь не запрещено, а отложено — счётчик «N чтений на серию» читался бы как «после пятого
можно».
"""
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from claude_.hook.claude_hook import claude_hook_input, claude_hook_is_tool, claude_hook_wanted  # noqa: E402
from claude_.hook.claude_hook_dsh_nudge import claude_hook_dsh_nudge_is_read  # noqa: E402
from claude_.hook.gate.claude_gate import claude_gate_off  # noqa: E402
from dsh_.dsh_wait import dsh_wait_answered, dsh_wait_key, dsh_wait_marks  # noqa: E402

# Инструменты, на которые поставлена застава; судит по ним `…_is_read`, а не регистр имён.
CLAUDE_GATE_DSH_ALIVE_TOOLS = ('Read', 'Grep', 'Glob', 'Bash')

# Клапан, снимающий заставу целиком: имена свои и длинные (claude_gate.md, грабля 3).
CLAUDE_GATE_DSH_ALIVE_PASS = 'CLAUDE_GATE_DSH_ALIVE_PASS'

# Что можно читать и пока ждут: ответы ждуна, память и настройки Claude, файлы DSH.
CLAUDE_GATE_DSH_ALIVE_ROOTS = ('/tmp', '~/.claude', '~/.dsh')

# Токен с расширением на конце — похоже на относительный путь-файл (`x.py`), голое слово — нет.
CLAUDE_GATE_DSH_ALIVE_EXT_RE = re.compile(r'\.\w{1,6}$')

CLAUDE_GATE_DSH_ALIVE_WAIT_TEXT = (
    'Читать код нельзя: у этой сессии работает субагент DSH — {cards}, ответа нет, ждём {waited} '
    'с момента отдачи. Запусти фоном\n'
    '  dsh-board wait --out-dir {out}    (run_in_background: true) — его выход разбудит по ответам,\n'
    'и поставь сторож живости карточек раз в 10 минут (`dsh-board working`): ждун молчит, '
    'когда карточка встаёт. Пока ждёшь — только то, что от ответа не зависит; '
    'читать код самой — когда субагент перестанет работать.')

CLAUDE_GATE_DSH_ALIVE_TAKE_TEXT = (
    'Читать код нельзя: ответ субагента DSH — {cards} — уже пришёл, ждать нечего — '
    'забери его: dsh-board wait / collect / inbox\n'
    '  dsh-board wait --out-dir {out}    # по меткам, разберёт всё пришедшее\n'
    '  dsh-board inbox --take            # или из «входящих» напрямик\n'
    '  dsh-board collect <каталог>       # если это был оборванный fanout\n'
    'Метки опустеют — чтения снова свободны.')


def claude_gate_dsh_alive_check(data: dict, marks: dict = None, answered: list = None,
                                working: list = None) -> dict:
    """Отменять ли этот вызов и почему.

    Args:
        data: ввод хука `PreToolUse` (`tool_name`, `tool_input`, `session_id`, …).
        marks: метки {карточка: {'owner','at'}}; нет — читаются `dsh_wait_marks` (в тесте — готовые).
        answered: карточки с пришедшим и не забранным ответом, строки `dsh_wait_answered`; нет — хост.
        working: живые карточки, строки с `session`/`title`; нет — опрос хоста.

    Returns:
        dict: `{'block', 'mode', 'cards', 'why'}`; `mode` — `wait` (субагент работает) или
        `take` (у всех карточек ответ пришёл и не забран); `cards` — [{key, title, age}] для текста.

    Живой субагент — карточка из меток этой сессии, у которой ответа ещё нет и которая
    занимает мотор доски или у которой идёт круг (`running`). Признака «в очереди» отдельно
    не достать: очередь промптов карточки наружу не видна, но пока в ней что-то разбирают,
    карточка `running`, а промпты `dsh-board` идут мимо очереди вовсе (`docs/dsh_board.md`,
    §1, §3). Круг, отданный секунду назад, ещё не дал ни одного признака — один вызов
    застава может пропустить, следующий — нет.
    """
    session = str((data or {}).get('session_id') or '')

    if claude_gate_off(CLAUDE_GATE_DSH_ALIVE_PASS):
        return {'block': False, 'mode': '', 'cards': [], 'why': f'снята через {CLAUDE_GATE_DSH_ALIVE_PASS}'}

    if marks is None:
        marks = dsh_wait_marks(session)
    waiting = {key: row for key, row in (marks or {}).items()
               if isinstance(row, dict) and row.get('owner') == session}
    if not waiting:
        return {'block': False, 'mode': '', 'cards': [], 'why': 'эта сессия ответов не ждёт'}

    if not claude_hook_dsh_nudge_is_read(_call(data)):
        return {'block': False, 'mode': '', 'cards': [], 'why': 'это не чтение: правка, запись или dsh-board'}

    if _reads_own_files(data):
        return {'block': False, 'mode': '', 'cards': [], 'why': 'чтение своих файлов, а не кода'}

    if answered is None:
        answered = dsh_wait_answered(sorted(waiting))
    got = {dsh_wait_key(str(row.get('session') or '')) for row in answered or []} & set(waiting)
    pending = set(waiting) - got
    if pending and working is None:
        working = _working(pending)
    alive = [row for row in working or [] if dsh_wait_key(str(row.get('session') or '')) in pending]

    if alive:
        return {'block': True, 'mode': 'wait', 'session': session, 'cards': _cards(alive, waiting),
                'why': 'субагент работает, ответа ещё нет'}

    if got:
        rows = [row for row in answered or [] if dsh_wait_key(str(row.get('session') or '')) in got]
        return {'block': True, 'mode': 'take', 'session': session, 'cards': _cards(rows, waiting),
                'why': 'ответ пришёл — его надо забрать'}

    return {'block': False, 'mode': '', 'cards': [], 'why': 'карточки не работают — действуй сама'}


def claude_gate_dsh_alive_format(verdict: dict) -> str:
    """Причина отмены для `permissionDecisionReason`; пусто — пропустить."""
    verdict = verdict or {}
    if not verdict.get('block'):
        return ''
    cards = ', '.join(f"«{row['title'] or row['key'][:8]}» ({row['key'][:8]}, ждём {_human(row['age'])})"
                      for row in verdict['cards'])
    waited = _human(max((row['age'] for row in verdict['cards']), default=0))
    out = os.path.join(os.environ.get('TMPDIR') or '/tmp', f"dsh-wait-{str(verdict.get('session'))[:8]}")
    text = CLAUDE_GATE_DSH_ALIVE_WAIT_TEXT if verdict['mode'] == 'wait' else CLAUDE_GATE_DSH_ALIVE_TAKE_TEXT

    return text.format(cards=cards, waited=waited, out=out)


def main() -> int:
    """Хук `PreToolUse`: печатью `permissionDecision: deny` отменяет вызов; молчание пропускает.

    ⚠ Код возврата всегда 0: двойка показала бы текст как ошибку хука, а здесь отказ —
    решение заставы, оформленное по договору `PreToolUse`.
    """
    data = claude_hook_input()

    if not claude_hook_wanted(data, CLAUDE_GATE_DSH_ALIVE_TOOLS):
        return 0

    try:
        said = claude_gate_dsh_alive_format(claude_gate_dsh_alive_check(data))
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


def _reads_own_files(data: dict) -> bool:
    """Вызов трогает только свои файлы: ответы ждуна, память Claude, метки DSH — не код."""
    tool_input = (data or {}).get('tool_input') or {}

    if claude_hook_is_tool((data or {}).get('tool_name'), ('Read', 'Grep', 'Glob')):
        path = str(tool_input.get('file_path') or tool_input.get('path') or '')

        return bool(path) and _under_own_root(path)

    return _bash_reads_own_only(str(tool_input.get('command') or ''))


def _bash_reads_own_only(command: str) -> bool:
    """В команде все аргументы-пути — под своими корнями, относительных путей-файлов нет.

    Абсолютных токенов нет вовсе — пропуск не положен: тогда команда читает «как обычно»,
    из рабочего каталога проекта (`ls`, `grep -rn "…"` без путей).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    paths = [token for token in tokens if token[:1] in ('/', '~')]

    if not paths:
        return False

    return all(_under_own_root(token) for token in paths) \
        and not any(_looks_like_path(token) for token in tokens if token[:1] not in ('/', '~'))


def _looks_like_path(word: str) -> bool:
    """Токен похож на путь-файл, а не на слово-аргумент: разделитель, точка в начале, расширение."""
    if word.startswith('-'):
        return False

    return word in ('.', '..') or word.startswith(('./', '../')) or '/' in word \
        or bool(CLAUDE_GATE_DSH_ALIVE_EXT_RE.search(word))


def _under_own_root(candidate: str) -> bool:
    try:
        got = Path(os.path.realpath(os.path.expanduser(str(candidate))))
    except OSError:
        return False

    return any(got == root or root in got.parents for root in _own_roots())


def _own_roots() -> tuple:
    """Свои корни, вычисленные на ходу: тесты подменяют `HOME`, а не просят пересобрать модуль."""
    return tuple(Path(os.path.realpath(os.path.expanduser(one))) for one in CLAUDE_GATE_DSH_ALIVE_ROOTS)


def _working(keys: set) -> list:
    """Наши карточки, что ещё работают: занимают мотор доски или у карточки идёт круг."""
    from dsh_.dsh_board import dsh_board_motors_busy, dsh_board_sessions  # ленивый: хост спрашиваем, только когда нужно

    rows = {dsh_wait_key(str(row.get('session') or '')): row
            for row in dsh_board_motors_busy().get('working') or []}

    for row in dsh_board_sessions(include_idle=True):
        key = dsh_wait_key(str(row.get('session') or ''))

        if row.get('running') and key not in rows:
            rows[key] = row

    return [row for key, row in rows.items() if key in keys]


def _cards(rows: list, marks: dict) -> list:
    """Карточки для текста: имя, ключ и сколько секунд ждут с момента отдачи."""
    now, out = time.time(), []

    for row in rows:
        key = dsh_wait_key(str(row.get('session') or ''))
        out.append({'key': key, 'title': str(row.get('title') or ''),
                    'age': now - (marks.get(key) or {}).get('at', now)})

    return out


def _human(seconds: float) -> str:
    minutes = int(max(0.0, float(seconds or 0)) // 60)

    return f'{minutes} мин' if minutes < 60 else f'{minutes // 60} ч {minutes % 60} мин'

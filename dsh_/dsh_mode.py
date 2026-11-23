"""
Режим работы оркестратора с субагентами DSH: `high`, `free`, `off`.

Раньше «отдавать ли субагенту» решала модель заново на каждой задаче — по совету из
CLAUDE.md, — и решение гуляло: исследования уходили на ферму целиком, а разбор кода
той же сессии делался руками. Режим переносит это решение на человека, а
напоминание о нём — с памяти модели на механику: хук `UserPromptSubmit`
(`claude_/hook/claude_hook_dsh_mode.py`) кладёт правило режима в контекст каждого хода.

- `high` — по умолчанию: отдаётся всё, кроме короткого списка «только сам»;
- `free` — на усмотрение модели: отдаётся объёмное;
- `off`  — субагентов не звать (ферма занята, лежит или нужен быстрый ответ).

Режим — на сессию Claude Code (`~/.cache/dsh-mode/<сессия>`), умолчание — общий файл
`~/.claude/dsh-mode`; нет и его — `high`. Переключает человек: `/dsh high|free|off`
(хук ловит это сообщение сам и пишет файл сессии) или руками:

    dsh-board mode                       # режим этой сессии
    dsh-board mode free --session <id>   # сессии
    dsh-board mode off --default         # умолчание для всех новых
"""
import os
import re

DSH_MODE_HIGH = 'high'
DSH_MODE_FREE = 'free'
DSH_MODE_OFF = 'off'
DSH_MODE_ALL = (DSH_MODE_HIGH, DSH_MODE_FREE, DSH_MODE_OFF)
DSH_MODE_DEFAULT = DSH_MODE_HIGH
DSH_MODE_DIR = '~/.cache/dsh-mode'
DSH_MODE_DEFAULT_FILE = '~/.claude/dsh-mode'

# Сообщение-переключатель: `/dsh high`, `dsh free`, `/dsh off`; регистр не важен.
DSH_MODE_SWITCH_RE = re.compile(r'^\s*/?dsh\s+(high|free|off)\s*$', re.IGNORECASE)

# Чего не отдают ни в каком режиме — правила проекта, а не вкус режима.
DSH_MODE_NEVER = ('коммиты и пуш, прод и выкладка, любые необратимые действия; '
                  'решения об архитектуре; вопросы человеку и общение с ним; '
                  'окончательная приёмка результата')

DSH_MODE_RULES = {
    DSH_MODE_HIGH: (
        'Режим DSH этой сессии: HIGH — максимум работы субагентам DeepSeek (dsh-board). '
        'По умолчанию ОТДАВАЙ: разведку и чтение кода, поиск причины, правки кода '
        '(только `--worktree`, принимать `land`), прогон тестов, ревью, разбор логов и длинных '
        'выводов, поиск в интернете, сводки; проверку их ответов — `dsh-board verify`. '
        'Независимые куски — разом через `fanout`. '
        f'Сам делай только: {DSH_MODE_NEVER}; и работу в одну-две команды, где описать '
        'задачу дольше, чем сделать. Делаешь объёмное сам — скажи человеку одной строкой почему.'),
    DSH_MODE_FREE: (
        'Режим DSH этой сессии: FREE — субагентов DeepSeek (dsh-board) зови на своё '
        'усмотрение: объёмное и не требующее решений (обзор кода, ревью, логи, поиск, сводки) '
        f'— им, остальное сам. Никогда не отдавай: {DSH_MODE_NEVER}.'),
    DSH_MODE_OFF: (
        'Режим DSH этой сессии: OFF — субагентов DeepSeek НЕ зови: ни new/ask, ни fanout, '
        'ни verify; работай сам. Уже идущих субагентов дождись как обычно (`dsh-board wait`). '
        'Префикс «d » в сообщении человека — явная просьба, она режим перекрывает.'),
}


def dsh_mode_get(session: str = '') -> str:
    """Режим сессии; нет своего — общее умолчание; нет и его — `high`."""
    for path in (_session_path(session) if session else '', os.path.expanduser(DSH_MODE_DEFAULT_FILE)):
        mode = _read(path) if path else ''
        if mode:
            return mode
    return DSH_MODE_DEFAULT


def dsh_mode_set(mode: str, session: str = '', default: bool = False) -> str:
    """Записать режим сессии (или общее умолчание при `default`); вернуть путь файла."""
    mode = (mode or '').strip().lower()
    if mode not in DSH_MODE_ALL:
        raise ValueError(f'режим: {", ".join(DSH_MODE_ALL)}, а не {mode!r}')
    if default:
        path = os.path.expanduser(DSH_MODE_DEFAULT_FILE)
    elif session:
        path = _session_path(session)
    else:
        raise ValueError('чей режим: --session <id> или --default')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write(mode + '\n')
    os.replace(tmp, path)
    return path


def dsh_mode_switch(prompt: str) -> str:
    """Режим из сообщения-переключателя `/dsh <режим>`; не переключатель — пусто."""
    match = DSH_MODE_SWITCH_RE.match(prompt or '')
    return match.group(1).lower() if match else ''


def dsh_mode_rule(mode: str) -> str:
    """Правило режима для контекста модели."""
    return DSH_MODE_RULES.get(mode, DSH_MODE_RULES[DSH_MODE_DEFAULT])


def _session_path(session: str) -> str:
    # Имя сессии идёт в путь: только безопасные знаки, как в статусной строке.
    safe = re.sub(r'[^A-Za-z0-9-]', '', session)
    if not safe:
        raise ValueError(f'id сессии без допустимых знаков: {session!r}')
    return os.path.join(os.path.expanduser(DSH_MODE_DIR), safe)


def _read(path: str) -> str:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            mode = handle.read(16).strip().lower()
    except OSError:
        return ''
    return mode if mode in DSH_MODE_ALL else ''

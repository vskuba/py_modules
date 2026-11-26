"""Сменить бэкенд сессии Claude Code кликом из статусной строки: подписка Claude ↔ чужая.

Сам бэкенд — переменные `ANTHROPIC_*` окружения `claude`, и на ходу Claude Code их не
перечитывает: сменить можно только перезапуском с `--resume` той же беседы. Перезапускать
умеет обёртка claude-shell — она родитель `claude`. Поэтому клик не меняет ничего сам, а
передаёт обёртке просьбу: файл с именем бэкенда и сигнал `SIGUSR1`. Что значит имя
(адрес, ключ, модели), знает только обёртка — каталог бэкендов живёт у неё.

⚠ Голый `claude` (не под claude-shell) переключить нечем: перезапускать его некому.
"""
import os
import signal

# Просьбы обёрткам: файл на pid обёртки, внутри — имя бэкенда.
CLAUDE_SESSION_BACKEND_DIR = '~/.cache/claude-shell-backend'

# Имя процесса обёртки в /proc/<pid>/comm.
CLAUDE_SESSION_BACKEND_SHELL = 'claude-shell'

# Переменная окружения `claude`, которой обёртка говорит «умею менять бэкенд»; в ней же —
# имя текущего (её читает статусная строка).
CLAUDE_SESSION_BACKEND_MARK = 'CLAUDE_SHELL_BACKEND'


def claude_session_backend_request(claude_pid: int, backend: str) -> str:
    """Попросить обёртку процесса `claude_pid` перезапустить его на бэкенде `backend`.

    Args:
        claude_pid: pid процесса `claude`, чья статусная строка нажата.
        backend: имя бэкенда из каталога claude-shell (`claude`, `qwen`).

    Returns:
        Пусто — просьба передана; иначе причина отказа словами.
    """
    if not backend.isidentifier():
        return f'неизвестное имя бэкенда «{backend}»'
    shell_pid = _claude_session_backend_parent(claude_pid)
    if not shell_pid:
        return (f'claude {claude_pid} запущен не под claude-shell или под старым, '
                f'без переключателя бэкенда — перезапустить его некому')
    folder = os.path.expanduser(CLAUDE_SESSION_BACKEND_DIR)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, str(shell_pid))
    with open(path + '.tmp', 'w', encoding='utf-8') as handle:
        handle.write(backend)
    os.replace(path + '.tmp', path)
    try:
        os.kill(shell_pid, signal.SIGUSR1)
    except OSError as error:
        return f'claude-shell {shell_pid} не принял сигнал: {error}'
    return ''


def claude_session_backend_take(shell_pid: int) -> str:
    """Забрать просьбу, оставленную обёртке `shell_pid`. Пусто — просьбы нет."""
    path = os.path.join(os.path.expanduser(CLAUDE_SESSION_BACKEND_DIR), str(shell_pid))
    try:
        with open(path, encoding='utf-8') as handle:
            backend = handle.read().strip()
        os.remove(path)
    except OSError:
        return ''
    return backend


def _claude_session_backend_parent(claude_pid: int) -> int:
    # Родитель `claude` — обёртка claude-shell, умеющая менять бэкенд? Иначе 0.
    # ⚠⚠ Умение — по `CLAUDE_SESSION_BACKEND_MARK` в окружении `claude`: его кладёт только
    # обёртка с обработчиком `SIGUSR1`. Обёртке старше — сигнал по умолчанию убивает её
    # вместе с сессией.
    try:
        with open(f'/proc/{int(claude_pid)}/stat', encoding='utf-8') as handle:
            parent = int(handle.read().rsplit(')', 1)[1].split()[1])
        with open(f'/proc/{parent}/comm', encoding='utf-8') as handle:
            name = handle.read().strip()
        with open(f'/proc/{int(claude_pid)}/environ', 'rb') as handle:
            names = {line.split(b'=', 1)[0] for line in handle.read().split(b'\0')}
        marked = CLAUDE_SESSION_BACKEND_MARK.encode() in names
    except (OSError, ValueError, IndexError):
        return 0
    return parent if name == CLAUDE_SESSION_BACKEND_SHELL and marked else 0

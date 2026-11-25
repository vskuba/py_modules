"""tmux снаружи: какие панели есть, в какой из них живёт процесс, вывести панель на экран.

```bash
PYTHONPATH=. python -m tmux_.tmux_ panes            # все панели всех сессий
PYTHONPATH=. python -m tmux_.tmux_ pane-of PID      # панель процесса (или пусто)
PYTHONPATH=. python -m tmux_.tmux_ focus ЦЕЛЬ       # показать панель: session:window.pane
PYTHONPATH=. python -m tmux_.tmux_ window СЕССИЯ ПАПКА КОМАНДА…   # новое окно с программой
```

⚠ Процесс в панели — не сама панель: tmux знает только `pane_pid` (обычно оболочку),
а программа в ней — потомок. Панель процесса ищется подъёмом по родителям в `/proc`.
⚠ `focus` снаружи tmux (из сервиса, из обработчика ссылки) обязан назвать клиента:
`switch-client` без `-c` берёт «текущего», а у внешнего процесса его нет.
"""
import shutil
import subprocess
import sys

# Сколько шагов вверх по родителям искать панель: дальше — уже не потомок панели.
TMUX_PARENT_DEPTH = 12


def tmux_panes() -> list:
    """Все панели tmux: `[{pane_pid, target, session, window, name, attached}]`.

    `attached` — к сессии tmux подключён хоть один клиент, то есть панель где-то на экране.

    tmux не стоит или сервер не запущен — пустой список, не исключение.
    """
    if not shutil.which('tmux'):
        return []
    out = _tmux_run('list-panes', '-a', '-F',
                    '#{pane_pid}\t#{session_name}\t#{window_index}\t#{pane_index}\t'
                    '#{window_name}\t#{session_attached}')
    rows = []
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) != 6 or not parts[0].isdigit():
            continue
        pid, session, window, pane, name, attached = parts
        rows.append({'pane_pid': int(pid), 'target': f'{session}:{window}.{pane}',
                     'session': session, 'window': window, 'name': name,
                     'attached': attached not in ('', '0')})
    return rows


def tmux_pane_of_pid(pid: int, panes: list = None) -> str:
    """Панель, в которой работает процесс `pid` (он сам или его предок — процесс панели).

    Returns:
        Цель `session:window.pane` или пустая строка, если процесс не в tmux.
    """
    by_pid = {row['pane_pid']: row['target'] for row in (tmux_panes() if panes is None else panes)}
    current = int(pid)
    for _ in range(TMUX_PARENT_DEPTH):
        if current in by_pid:
            return by_pid[current]
        current = _tmux_parent(current)
        if current <= 1:
            break
    return ''


def tmux_focus(target: str) -> bool:
    """Вывести панель на экран: самый недавно активный клиент переключается на её окно.

    Returns:
        True — переключено; False — нет клиента или нет такой цели.
    """
    clients = _tmux_run('list-clients', '-F', '#{client_activity}\t#{client_name}').splitlines()
    clients = sorted((line.split('\t') for line in clients if '\t' in line), reverse=True)
    if not clients:
        return False
    session_window = target.rsplit('.', 1)[0]
    ok = _tmux_ok('switch-client', '-c', clients[0][1], '-t', session_window)
    ok = _tmux_ok('select-window', '-t', session_window) and ok
    return _tmux_ok('select-pane', '-t', target) and ok


def tmux_window_new(session: str, cwd: str, command: list, name: str = '') -> bool:
    """Новое окно в сессии tmux с программой — оно сразу показывается её клиентам.

    ⚠ Программа получает PATH **вызывающего** (клиента tmux), а не сервера: из службы
    без nvm голое `claude` не находится, окно закрывается молча, а вызов отвечает успехом.
    Программы из PATH человека — через `bash -ic '…'`.
    """
    args = ['new-window', '-t', f'{session}:', '-c', cwd]
    if name:
        args += ['-n', name]
    return _tmux_ok(*args, *command)


def _tmux_run(*args) -> str:
    try:
        return subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def _tmux_ok(*args) -> bool:
    try:
        return subprocess.run(['tmux', *args], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _tmux_parent(pid: int) -> int:
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8', errors='replace') as handle:
            stat = handle.read()
    except OSError:
        return 0
    # Имя процесса в скобках может содержать пробелы — поля считаются после последней «)».
    fields = stat.rsplit(')', 1)[-1].split()
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'panes'
    if command == 'panes':
        for row in tmux_panes():
            print(f"{row['target']:<20} pid {row['pane_pid']:<8} {row['name']}")
    elif command == 'pane-of' and len(sys.argv) > 2:
        print(tmux_pane_of_pid(int(sys.argv[2])))
    elif command == 'window' and len(sys.argv) > 4:
        sys.exit(0 if tmux_window_new(sys.argv[2], sys.argv[3], sys.argv[4:]) else 1)
    elif command == 'focus' and len(sys.argv) > 2:
        sys.exit(0 if tmux_focus(sys.argv[2]) else 1)
    else:
        print(__doc__)
        sys.exit(2)

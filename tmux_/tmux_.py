"""tmux снаружи: какие панели есть, в какой из них живёт процесс, вывести панель на экран.

```bash
PYTHONPATH=. python -m tmux_.tmux_ panes            # все панели всех сессий
PYTHONPATH=. python -m tmux_.tmux_ pane-of PID      # панель процесса (или пусто)
PYTHONPATH=. python -m tmux_.tmux_ focus ЦЕЛЬ       # показать панель: session:window.pane
PYTHONPATH=. python -m tmux_.tmux_ window СЕССИЯ ПАПКА КОМАНДА…   # новое окно с программой
PYTHONPATH=. python -m tmux_.tmux_ enter БАЗА ПАПКА КОМАНДА…    # запустить в tmux здесь
```

⚠ Процесс в панели — не сама панель: tmux знает только `pane_pid` (обычно оболочку),
а программа в ней — потомок. Панель процесса ищется подъёмом по родителям в `/proc`.
⚠ `focus` снаружи tmux (из сервиса, из обработчика ссылки) обязан назвать клиента:
`switch-client` без `-c` берёт «текущего», а у внешнего процесса его нет.
"""
import os
import shutil
import subprocess
import sys

from proc_.proc_ps import proc_ps_ancestors

# Сколько шагов вверх по родителям искать панель: дальше — уже не потомок панели.
TMUX_PARENT_DEPTH = 12


def tmux_panes() -> list:
    """Все панели tmux: `[{pane_pid, target, session, window, name, attached, title}]`.

    `title` — заголовок панели: его ставит программа в ней (OSC 0/2), он же — шапка вкладки
    терминала (`set-titles-string "#T"`).

    `attached` — к сессии tmux подключён хоть один клиент, то есть панель где-то на экране.

    tmux не стоит или сервер не запущен — пустой список, не исключение.
    """
    if not shutil.which('tmux'):
        return []
    out = _tmux_run('list-panes', '-a', '-F',
                    '#{pane_pid}\t#{session_name}\t#{window_index}\t#{pane_index}\t'
                    '#{window_name}\t#{session_attached}\t#{pane_title}')
    rows = []
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) != 7 or not parts[0].isdigit():
            continue
        pid, session, window, pane, name, attached, title = parts
        rows.append({'pane_pid': int(pid), 'target': f'{session}:{window}.{pane}',
                     'session': session, 'window': window, 'name': name,
                     'attached': attached not in ('', '0'), 'title': title})
    return rows


def tmux_pane_of_pid(pid: int, panes: list = None) -> str:
    """Панель, в которой работает процесс `pid` (он сам или его предок — процесс панели).

    Returns:
        Цель `session:window.pane` или пустая строка, если процесс не в tmux.
    """
    by_pid = {row['pane_pid']: row['target'] for row in (tmux_panes() if panes is None else panes)}
    for current in [int(pid), *proc_ps_ancestors(pid, TMUX_PARENT_DEPTH)]:
        if current in by_pid:
            return by_pid[current]
    return ''


def tmux_clients() -> list:
    """Подключённые клиенты tmux — окна терминалов, где tmux на экране, свежие сверху.

    Returns:
        `[{pid, name, session, activity}]`: `pid` — процесс клиента (его предки — терминал
        или IDE, где он открыт), `session` — какую сессию tmux он показывает.
    """
    rows = []
    for line in _tmux_run('list-clients', '-F',
                          '#{client_pid}\t#{client_name}\t#{session_name}\t#{client_activity}').splitlines():
        parts = line.split('\t')
        if len(parts) == 4 and parts[0].isdigit():
            rows.append({'pid': int(parts[0]), 'name': parts[1], 'session': parts[2],
                         'activity': int(parts[3]) if parts[3].isdigit() else 0})
    return sorted(rows, key=lambda row: -row['activity'])


def tmux_focus(target: str) -> bool:
    """Вывести панель на экран: её окно выбирается в сессии tmux, клиент переводится на неё.

    Клиент — тот, что уже показывает сессию цели (вкладка, где она открыта); такого нет —
    самый недавно активный: он переключается на чужую сессию.

    Returns:
        True — переключено; False — нет клиента или нет такой цели.
    """
    clients = tmux_clients()
    if not clients:
        return False
    session_window = target.rsplit('.', 1)[0]
    session = session_window.split(':', 1)[0]
    client = next((row for row in clients if row['session'] == session), clients[0])
    ok = _tmux_ok('switch-client', '-c', client['name'], '-t', session_window)
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


def tmux_pane_title(target: str) -> str:
    """Заголовок панели сейчас — как его поставила программа в ней; нет панели — пусто."""
    return _tmux_run('display-message', '-p', '-t', target, '#{pane_title}').strip()


def tmux_pane_kill(target: str) -> bool:
    """Закрыть панель tmux вместе с её программой (SIGHUP всему, что в ней); окно без панелей
    закрывается само."""
    return _tmux_ok('kill-pane', '-t', target)


def tmux_session_enter(base: str, cwd: str, command: list, name: str = '') -> list:
    """Запустить программу в tmux прямо в этом терминале — команда, которую надо `exec`.

    Сессия `base` свободна (её не показывает ни один клиент) — программа становится её новым
    окном, и терминал к ней подключается; сессии нет — она заводится. Занята другим
    терминалом — заводится своя `base-2`, `base-3`, …: иначе второй терминал показывал бы
    первый зеркалом.

    Returns:
        argv для `os.execvp` (`tmux attach …` / `tmux new-session …`).
    """
    names = set(_tmux_run('list-sessions', '-F', '#{session_name}').split())
    shown = {row['session'] for row in tmux_clients()}
    window = ['-n', name] if name else []
    if base in names and base not in shown:
        _tmux_ok('new-window', '-t', f'{base}:', '-c', cwd, *window, *command)
        return ['tmux', 'attach', '-t', base]
    session = base
    number = 2
    while session in names:
        session, number = f'{base}-{number}', number + 1
    return ['tmux', 'new-session', '-s', session, '-c', cwd, *window, *command]


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


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'panes'
    if command == 'panes':
        for row in tmux_panes():
            print(f"{row['target']:<20} pid {row['pane_pid']:<8} {row['name']}")
    elif command == 'pane-of' and len(sys.argv) > 2:
        print(tmux_pane_of_pid(int(sys.argv[2])))
    elif command == 'window' and len(sys.argv) > 4:
        sys.exit(0 if tmux_window_new(sys.argv[2], sys.argv[3], sys.argv[4:]) else 1)
    elif command == 'enter' and len(sys.argv) > 4:
        argv = tmux_session_enter(sys.argv[2], sys.argv[3], sys.argv[4:])
        os.execvp(argv[0], argv)
    elif command == 'focus' and len(sys.argv) > 2:
        sys.exit(0 if tmux_focus(sys.argv[2]) else 1)
    else:
        print(__doc__)
        sys.exit(2)

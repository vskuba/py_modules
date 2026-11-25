"""Где открыта сессия Claude Code: в какой IDE или терминале её видно — PyCharm, PhpStorm, …

Ответ — по цепочке предков процесса (`proc_ps_ancestors`): сессия — потомок оболочки во
вкладке терминала, а та — потомок IDE. Узнаётся по пути исполняемого файла предка
(`/snap/phpstorm/733/bin/phpstorm` → PhpStorm).

⚠ Сессия в tmux — потомок сервера tmux, а не IDE: смотрятся предки **клиента** tmux,
который показывает её сессию (`tmux_clients`), — вкладка, где tmux открыт.
"""
import os

from proc_.proc_ps import proc_ps_ancestors
from tmux_.tmux_ import tmux_clients

# Приметы приложений в пути исполняемого файла предка: первая совпавшая — ответ.
CLAUDE_SESSION_HOST_APPS = (
    ('phpstorm', 'PhpStorm'), ('pycharm', 'PyCharm'), ('goland', 'GoLand'),
    ('webstorm', 'WebStorm'), ('rider', 'Rider'), ('clion', 'CLion'),
    ('rustrover', 'RustRover'), ('intellij', 'IDEA'), ('/idea', 'IDEA'),
    ('/code', 'VS Code'), ('cursor', 'Cursor'), ('gnome-terminal', 'Терминал'),
    ('ptyxis', 'Терминал'), ('konsole', 'Konsole'), ('kitty', 'kitty'),
    ('alacritty', 'Alacritty'), ('wezterm', 'WezTerm'),
)


def claude_session_host(pid: int, tmux_session: str = '', clients: list = None) -> str:
    """Приложение, где видна сессия: «PyCharm», «PhpStorm», «Терминал»…; не узнано — пусто.

    Args:
        pid: процесс сессии Claude Code.
        tmux_session: сессия tmux, если Claude живёт в ней, — тогда смотрится её клиент.
        clients: готовый `tmux_clients()`, чтобы не спрашивать tmux на каждую строку.
    """
    start = int(pid)
    if tmux_session:
        clients = tmux_clients() if clients is None else clients
        client = next((row for row in clients if row['session'] == tmux_session), None)
        if not client:
            return ''
        start = client['pid']
    for ancestor in [start, *proc_ps_ancestors(start)]:
        try:
            exe = os.readlink(f'/proc/{ancestor}/exe').lower()
        except OSError:
            continue
        for mark, app in CLAUDE_SESSION_HOST_APPS:
            if mark in exe:
                return app
    return ''

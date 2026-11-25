"""Вывести окно вперёд по заголовку в GNOME на Wayland — через расширение claude-window@local.

На Wayland программа не может поднять ни своё окно, ни чужое (защита от кражи фокуса),
`xdotool` видит только окна X11. Право есть у оболочки: расширение
`claude-shell/gnome/claude-window@local` даёт по D-Bus `Activate(заголовок, класс)` и `List()`.

```bash
PYTHONPATH=. python -m gnome_.gnome_window list
PYTHONPATH=. python -m gnome_.gnome_window activate «часть заголовка» [класс]
```

⚠ Расширение, поставленное на Wayland, оболочка видит только после выхода из сеанса и
входа заново (`claude-shell/gnome/install.sh`). Нет расширения — вызовы отвечают «не вышло»,
а не падают.
"""
import json
import subprocess
import sys

# Адрес расширения на шине сеанса.
GNOME_WINDOW_DEST = 'org.gnome.Shell'
GNOME_WINDOW_PATH = '/org/gnome/Shell/Extensions/ClaudeWindow'
GNOME_WINDOW_IFACE = 'org.gnome.Shell.Extensions.ClaudeWindow'


def gnome_window_activate(title: str, wmclass: str = '') -> bool:
    """Вывести вперёд окно, в заголовке которого есть `title` (и в классе — `wmclass`).

    Returns:
        True — окно нашлось и поднято; False — нет окна или нет расширения.
    """
    return _gnome_window_call('Activate', title, wmclass).strip() == '(true,)'


def gnome_window_list() -> list:
    """Окна сеанса: `[{title, wmclass}]`; нет расширения — пусто."""
    out = _gnome_window_call('List').strip()
    if not out.startswith("('"):
        return []
    try:
        return json.loads(out[2:out.rindex("'")].encode().decode('unicode_escape').encode('latin-1').decode())
    except ValueError:
        return []


def _gnome_window_call(method: str, *args) -> str:
    try:
        return subprocess.run(['gdbus', 'call', '--session', '--dest', GNOME_WINDOW_DEST,
                               '--object-path', GNOME_WINDOW_PATH,
                               '--method', f'{GNOME_WINDOW_IFACE}.{method}', *args],
                              capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'list':
        for item in gnome_window_list():
            print(f"{item['wmclass']:<28} {item['title']}")
    elif len(sys.argv) > 2 and sys.argv[1] == 'activate':
        sys.exit(0 if gnome_window_activate(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else '') else 1)
    else:
        print(__doc__)
        sys.exit(2)

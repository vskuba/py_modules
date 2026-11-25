"""Переключение между сессиями Claude Code кликом: таблица со ссылками и сервер на петле.

Терминал JetBrains отдаёт наружу только `http://`-ссылки (свою схему вроде
`claude-session://` молча глотает — проверено), поэтому клик идёт так:
ссылка в статусной строке → браузер → `GET /switch/<sessionId>` этого сервера →
`tmux_focus` панели, где живёт сессия.

```bash
PYTHONPATH=. python -m claude_.session.claude_session_switch serve          # сервер
PYTHONPATH=. python -m claude_.session.claude_session_switch table [--current ID]
```

⚠ Переключить можно только сессию, запущенную внутри tmux; остальные в таблице есть,
но без ссылки.
⚠ Ответ на клик — `204 No Content`: часть браузеров тогда не оставляет пустую вкладку.
Не вышло переключить — страница с причиной, иначе клик «ничего не делает» молча.
"""
import argparse
import http.server
import html
import sys

from claude_.session.claude_session_live import claude_session_live_find, claude_session_live_list
from tmux_.tmux_ import tmux_focus

# Адрес сервера переключения: только петля.
CLAUDE_SESSION_SWITCH_HOST = '127.0.0.1'
CLAUDE_SESSION_SWITCH_PORT = 8797

# Сколько строк таблицы в статусной строке.
CLAUDE_SESSION_SWITCH_ROWS = 8

# Ширина колонки имени.
CLAUDE_SESSION_SWITCH_NAME = 34

# Вид статуса в таблице: цвет, значок, слово.
_CLAUDE_SESSION_SWITCH_LOOK = {'busy': ('\x1b[33m', '●', 'работает'),
                               'shell': ('\x1b[36m', '◐', 'фон'),
                               'idle': ('\x1b[90m', '○', 'ждёт')}


def claude_session_switch_to(session_id: str) -> str:
    """Вывести на экран панель tmux сессии `session_id`.

    Returns:
        Пусто — переключено; иначе причина отказа словами.
    """
    row = claude_session_live_find(session_id)
    if not row:
        return f'сессии {session_id} нет среди живых'
    if not row['pane']:
        return f"сессия «{row['name']}» запущена не в tmux — переключать некуда"
    if not tmux_focus(row['pane']):
        return f"tmux не переключил на {row['pane']}: нет подключённого клиента?"
    return ''


def claude_session_switch_table(current: str = '', rows: int = CLAUDE_SESSION_SWITCH_ROWS) -> str:
    """Таблица сессий для статусной строки: строка на сессию, имя — ссылкой на переключение.

    Args:
        current: `sessionId` окна, которое рисует строку, — помечается `▶`.
    """
    sessions = claude_session_live_list()
    lines = []
    for row in sessions[:rows]:
        color, mark, state = _CLAUDE_SESSION_SWITCH_LOOK.get(row['status'], _CLAUDE_SESSION_SWITCH_LOOK['idle'])
        mark = '▶' if row['sessionId'] == current else mark
        name = _claude_session_switch_short(row['name'], CLAUDE_SESSION_SWITCH_NAME)
        padded = name + ' ' * (CLAUDE_SESSION_SWITCH_NAME - len(name))
        if row['pane']:
            url = f"http://{CLAUDE_SESSION_SWITCH_HOST}:{CLAUDE_SESSION_SWITCH_PORT}/switch/{row['sessionId']}"
            padded = f'\x1b]8;;{url}\x1b\\{name}\x1b]8;;\x1b\\' + padded[len(name):]
        bg = ' bg' if row['kind'] == 'bg' else ''
        lines.append(f"  {color}{mark}\x1b[0m {padded} {color}{state:<8}\x1b[0m "
                     f"{_claude_session_switch_tokens(row['tokens']):>6}\x1b[90m{bg}\x1b[0m")
    if len(sessions) > rows:
        lines.append(f'  \x1b[90m… ещё {len(sessions) - rows}\x1b[0m')
    return '\n'.join(lines)


def claude_session_switch_serve(port: int = CLAUDE_SESSION_SWITCH_PORT) -> None:
    """Поднять сервер переключения на петле — держит `GET /switch/<sessionId>`."""
    http.server.ThreadingHTTPServer((CLAUDE_SESSION_SWITCH_HOST, port),
                                    _ClaudeSessionSwitchHandler).serve_forever()


def _claude_session_switch_short(text: str, limit: int) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _claude_session_switch_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f'{value / 1_000_000:.1f}M'
    return f'{value / 1000:.0f}k' if value >= 1000 else str(value)


class _ClaudeSessionSwitchHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith('/switch/'):
            self.send_response(404)
            self.end_headers()
            return
        error = claude_session_switch_to(self.path[len('/switch/'):].strip('/'))
        if not error:
            self.send_response(204)
            self.end_headers()
            return
        body = f'<meta charset="utf-8"><p>{html.escape(error)}</p>'.encode()
        self.send_response(409)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='переключение сессий Claude Code через tmux')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('serve').add_argument('--port', type=int, default=CLAUDE_SESSION_SWITCH_PORT)
    table = sub.add_parser('table')
    table.add_argument('--current', default='')
    table.add_argument('--rows', type=int, default=CLAUDE_SESSION_SWITCH_ROWS)
    sub.add_parser('to').add_argument('session')
    args = parser.parse_args()
    if args.command == 'serve':
        claude_session_switch_serve(args.port)
    elif args.command == 'table':
        print(claude_session_switch_table(args.current, args.rows))
    else:
        reason = claude_session_switch_to(args.session)
        print(reason or 'переключено')
        sys.exit(1 if reason else 0)

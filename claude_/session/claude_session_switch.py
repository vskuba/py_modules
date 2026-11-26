"""Переключение между сессиями Claude Code кликом: таблица со ссылками и сервер на петле.

Терминал JetBrains отдаёт наружу только `http://`-ссылки (свою схему вроде
`claude-session://` молча глотает — проверено), поэтому клик идёт так:
ссылка в статусной строке → браузер (или плагин IDE, без браузера) →
`GET /switch/<sessionId>` этого сервера → панель tmux сессии, а если она не в tmux —
вкладка терминала JetBrains с её заголовком (`jetbrains_tab_select`).

```bash
PYTHONPATH=. python -m claude_.session.claude_session_switch serve          # сервер
PYTHONPATH=. python -m claude_.session.claude_session_switch table [--rows N]
```

⚠ Не нашлось ни панели, ни вкладки — клик открывает страницу с причиной.
⚠ Ответ на клик — `204 No Content`: часть браузеров тогда не оставляет пустую вкладку.
Не вышло переключить — страница с причиной, иначе клик «ничего не делает» молча.
"""
import argparse
import http.server
import html
import os
import sys
import time
import urllib.parse

from claude_.session.claude_session_backend import claude_session_backend_request
from claude_.session.claude_session_close import claude_session_close_armed, claude_session_close_click
from claude_.session.claude_session_ready import claude_session_ready_seen
from claude_.session.claude_session_live import claude_session_live_find, claude_session_live_list, \
    claude_session_live_orphans
from dsh_.dsh_motor_badge import dsh_motor_badge, dsh_motor_badge_ask
from gnome_.gnome_window import gnome_window_activate
from jetbrains_.jetbrains_tab import jetbrains_tab_new, jetbrains_tab_select
from tmux_.tmux_ import tmux_focus, tmux_pane_title, tmux_window_new

# Адрес сервера переключения: только петля.
CLAUDE_SESSION_SWITCH_HOST = '127.0.0.1'
CLAUDE_SESSION_SWITCH_PORT = 8797

# Сколько ждать, пока шапка вкладки с tmux сменится на заголовок новой панели.
CLAUDE_SESSION_SWITCH_TITLE_DELAY = 0.4

# Чем запускается новая сессия: обёртка claude-shell (ответы через Telegram).
CLAUDE_SESSION_SWITCH_COMMAND = 'claude-shell'

# Журнал кликов: что пришло и чем кончилось — «клик ничего не сделал» разбирается по нему.
CLAUDE_SESSION_SWITCH_LOG = '~/.cache/claude-session-switch.log'

# Сколько строк таблицы в статусной строке.
CLAUDE_SESSION_SWITCH_ROWS = 8

# Ширина колонки имени.
CLAUDE_SESSION_SWITCH_NAME = 80

# Сколько знаков имени проекта перед заголовком.
CLAUDE_SESSION_SWITCH_PROJECT = 32

# Название свежей сессии, пока Claude Code не дал ей заголовок.
CLAUDE_SESSION_SWITCH_UNTITLED = 'Новая сессия'

# Сколько знаков вопроса сессии показывать в конце строки.
CLAUDE_SESSION_SWITCH_QUESTION = 60

# Значок приложения, где открыта сессия, — как логотип JetBrains: две буквы на плашке с
# переходом двух фирменных цветов (256-цветная палитра; картинок статусная строка не
# показывает). Ширина — 4 клетки у всех, чтобы колонка не плыла.
_CLAUDE_SESSION_SWITCH_BADGE = {
    'PyCharm': ('PC', 35, 220), 'PhpStorm': ('PS', 99, 205), 'GoLand': ('GO', 33, 135),
    'WebStorm': ('WS', 45, 33), 'IDEA': ('IJ', 205, 208), 'Rider': ('RD', 208, 161),
    'CLion': ('CL', 41, 33), 'RustRover': ('RR', 208, 45), 'VS Code': ('VS', 33, 39),
    'Cursor': ('CU', 245, 252), 'Терминал': ('>_', 240, 244), 'Konsole': ('>_', 240, 244),
    'kitty': ('>_', 240, 244), 'Alacritty': ('>_', 240, 244), 'WezTerm': ('>_', 240, 244),
}

# Радиокнопка строки: пустая у всех; своё окно статусная строка меняет на полную.
CLAUDE_SESSION_SWITCH_RADIO = '○'

# Вид статуса в таблице: цвет и слово.
_CLAUDE_SESSION_SWITCH_LOOK = {'ready': ('\x1b[1;92m', 'Ready'),
                               'waiting': ('\x1b[38;5;208m', 'вопрос'),
                               'busy': ('\x1b[33m', 'работает'),
                               'shell': ('\x1b[36m', 'фон'),
                               'idle': ('\x1b[90m', 'ждёт')}


def claude_session_switch_to(session_id: str) -> str:
    """Вывести сессию `session_id` на экран: панель tmux, а не в tmux — вкладку JetBrains.

    Вкладка IDE ищется по заголовку сессии (он же — шапка вкладки) плагином Claude Tab
    Switch (`jetbrains_tab_select`).

    Returns:
        Пусто — переключено; иначе причина отказа словами.
    """
    row = claude_session_live_find(session_id)
    if not row:
        return f'сессии {session_id} нет среди живых'
    claude_session_ready_seen(row['sessionId'], row['pid'])  # перешли на неё — «Ready» снят
    # Шапка вкладки — та, что стоит сейчас: у сессии без заголовка Claude Code пишет «Claude
    # Code»; среди одинаковых вкладок берётся вкладка проекта сессии.
    title = row['title'] or 'Claude Code'
    project = os.path.basename(row['cwd'].rstrip('/'))
    if row['pane']:
        if not tmux_focus(row['pane']):
            return f"tmux не переключил на {row['pane']}: нет подключённого клиента?"
        # tmux во вкладке JetBrains: её шапка — заголовок текущей панели (`set-titles`),
        # и после переключения она уже его носит. Не нашлась — tmux не в IDE, это не отказ.
        time.sleep(CLAUDE_SESSION_SWITCH_TITLE_DELAY)
        _claude_session_switch_raise(jetbrains_tab_select(tmux_pane_title(row['pane']) or title, project))
        return ''
    if _claude_session_switch_raise(jetbrains_tab_select(title, project)):
        return ''
    return (f"вкладки «{title}» нет ни в одной IDE с плагином Claude Tab Switch "
            f"и сессия «{row['name']}» не в tmux")


def claude_session_switch_new(cwd: str, tmux_session: str = '') -> str:
    """Новая сессия Claude Code: окном tmux в той же вкладке или новой вкладкой IDE.

    Args:
        cwd: папка проекта — там запустится Claude.
        tmux_session: окно, где нажали «+», само в tmux — новая сессия становится окном
            этой сессии tmux и сразу показывается в той же вкладке терминала; радиокнопка
            потом переключает между окнами. Пусто — вкладка терминала в IDE этого проекта.

    Returns:
        Пусто — открыта; иначе причина отказа словами.
    """
    if tmux_session:
        # Через интерактивную оболочку: окно берёт PATH того, кто его создаёт (служба без
        # nvm), и голый `claude` в нём не находился — окно молча закрывалось.
        shell = os.environ.get('SHELL') or '/bin/bash'
        # claude-shell, а не голый claude: в нём ответы на вопросы через Telegram.
        if tmux_window_new(tmux_session, cwd, [shell, '-ic', CLAUDE_SESSION_SWITCH_COMMAND],
                           os.path.basename(cwd.rstrip('/'))):
            return ''
        return f'tmux не открыл окно в сессии «{tmux_session}»'
    if jetbrains_tab_new(cwd):
        return ''
    return 'ни одной IDE с плагином Claude Tab Switch — открыть вкладку негде'


def claude_session_switch_table(rows: int = CLAUDE_SESSION_SWITCH_ROWS) -> str:
    """Таблица сессий для статусной строки: радиокнопка и имя — ссылками на переключение.

    Таблица общая на все окна: радиокнопку своей строки (`○` → `◉`) отмечает статусная
    строка окна сама — по `sessionId`, который есть в ссылке каждой строки.
    """
    # Только то, на что можно переключиться: фоновые и отсоединённые от экрана — мимо.
    every = claude_session_live_list()
    sessions = [row for row in every if row['visible']]
    # По алфавиту «проект: название»; группы — свой проект, своя IDE, чужая IDE — раскладывает
    # статусная строка окна (по `proj=`/`host=` в ссылке), порядок внутри группы — этот.
    sessions.sort(key=lambda row: (os.path.basename(row['cwd'].rstrip('/')).lower(),
                                   (row['title'] or CLAUDE_SESSION_SWITCH_UNTITLED).lower()))
    armed = claude_session_close_armed()
    lines = []
    for row in sessions[:rows]:
        color, state = _CLAUDE_SESSION_SWITCH_LOOK.get(row['status'], _CLAUDE_SESSION_SWITCH_LOOK['idle'])
        # «Ready» — ход закончен и ждёт человека; пока на сессию работают субагенты DSH,
        # ход по сути не закончен — это «фон», а не «Ready».
        if row.get('ready'):
            color, state = _CLAUDE_SESSION_SWITCH_LOOK['shell' if row['agents'] else 'ready']
        # «Проект: заголовок» — заголовок тот же, что в шапке вкладки; пока его нет (свежая
        # сессия) — «Новая сессия», а не служебное имя вроде «cyberai-61». Проект — имя папки.
        # Длинное имя проекта (cashier-service-payment-gateway) съело бы заголовок — короче.
        project = _claude_session_switch_short(os.path.basename(row['cwd'].rstrip('/')) or '—',
                                               CLAUDE_SESSION_SWITCH_PROJECT)
        name = _claude_session_switch_short(f"{project}: {row['title'] or CLAUDE_SESSION_SWITCH_UNTITLED}",
                                            CLAUDE_SESSION_SWITCH_NAME)
        padded = name + ' ' * (CLAUDE_SESSION_SWITCH_NAME - len(name))
        # Ссылка — у каждой строки: сессия не в tmux на клик ответит страницей с причиной,
        # а `sessionId` в строке нужен статусной строке, чтобы пометить своё окно.
        url = f"http://{CLAUDE_SESSION_SWITCH_HOST}:{CLAUDE_SESSION_SWITCH_PORT}/switch/{row['sessionId']}"
        padded = (_claude_session_switch_link(url, _claude_session_switch_project(name, project),
                                              row['host'], row['pid'], row['job'], row['cwd'], row['account'])
                  + padded[len(name):])
        # Название — на тёмной плашке во всю колонку, как кнопка «+»: видно, куда кликать;
        # по пробелу внутри слева и справа — отступы (сверху и снизу их в терминале нет).
        padded = f'\x1b[38;5;252;48;5;235m {padded} \x1b[0m'
        radio = _claude_session_switch_link(url, CLAUDE_SESSION_SWITCH_RADIO, row['host'], row['pid'], row['job'])
        close = _claude_session_switch_link(url.replace('/switch/', '/close/'),
                                            _claude_session_switch_cross(row['sessionId'] in armed),
                                            row['host'], row['pid'], row['job'])
        bg = ' bg' if row['kind'] == 'bg' else ''
        host = _claude_session_switch_badge(row['host'])
        # Строка начинается невидимым сбросом цвета: Claude Code срезает пробелы в начале
        # строки статуса, и без него отступ пропадал у всех строк, кроме подсвеченной своей.
        lines.append(f"\x1b[0m {close}  {radio} {padded}  {host}     {_claude_session_switch_state(color, state, row['question'])}\x1b[0m "
                     f"{_claude_session_switch_spent(row['tokens_in'], row['tokens_out'])} "
                     f"{_claude_session_switch_spent(row['window_in'], row['window_out'])} "
                     f"{_claude_session_switch_pct(row['window_pct'])}\x1b[90m{bg}\x1b[0m"
                     f"  {_claude_session_switch_agents(row['agents'])}"
                     f"{_claude_session_switch_question(row['question'])}")
    # Моторы, чей хозяин закрыт или скрыт, — внизу, тусклым: иначе работа пропала бы с глаз.
    for orphan in claude_session_live_orphans(every):
        # Та же сетка, что у строки сессии: крестика, радиокнопки, значка IDE и токенов нет —
        # пустые клетки их ширины; «закрыта»/«скрыта» — в колонке состояния.
        name = _claude_session_switch_short(
            f"{orphan['project'] or '—'}: {orphan['title'] or CLAUDE_SESSION_SWITCH_UNTITLED}",
            CLAUDE_SESSION_SWITCH_NAME)
        lines.append(f"\x1b[0m      \x1b[38;5;244m {name:<{CLAUDE_SESSION_SWITCH_NAME}} \x1b[0m"
                     f"{' ' * 11}\x1b[90m{orphan['state']:<8}\x1b[0m {' ' * 12} {' ' * 12} {' ' * 4}"
                     f"  {_claude_session_switch_agents(orphan['agents'])}")
    if len(sessions) > rows:
        lines.append(f'\x1b[0m  \x1b[90m… ещё {len(sessions) - rows}\x1b[0m')
    return '\n'.join(lines)


def claude_session_switch_serve(port: int = CLAUDE_SESSION_SWITCH_PORT) -> None:
    """Поднять сервер переключения на петле: `GET /switch/<sessionId>`, `GET /new?cwd=…`,
    `GET /backend?pid=<claude>&to=<бэкенд>`."""
    http.server.ThreadingHTTPServer((CLAUDE_SESSION_SWITCH_HOST, port),
                                    _ClaudeSessionSwitchHandler).serve_forever()


def _claude_session_switch_tmux_of(session_id: str) -> str:
    # Сессия tmux, в которой видна сессия `session_id`: своя панель или окно в фоновую
    # сессию (`job`). Видимое на экране — первым: окон в одну фоновую сессию бывает много.
    if not session_id:
        return ''
    rows = [row for row in claude_session_live_list()
            if row.get('pane') and (row['sessionId'] == session_id or row.get('job') == session_id)]
    rows.sort(key=lambda row: not row['visible'])
    return rows[0]['pane'].split(':')[0] if rows else ''


def _claude_session_switch_badge(host: str) -> str:
    badge = _CLAUDE_SESSION_SWITCH_BADGE.get(host)
    if not badge:
        return '\x1b[90m ·· \x1b[0m'
    letters, left, right = badge
    return (f'\x1b[1;38;5;16;48;5;{left}m {letters[0]}'
            f'\x1b[48;5;{right}m{letters[1]} \x1b[0m')


def _claude_session_switch_raise(ide_project: str) -> str:
    # Вкладка выбрана — окно IDE вперёд: на Wayland это может только оболочка, через
    # расширение claude-window@local (`gnome_window_activate`). Заголовок окна IDE содержит
    # имя проекта. Расширения нет — вкладка всё равно выбрана, окно остаётся, где было.
    if ide_project:
        gnome_window_activate(ide_project, 'jetbrains')
    return ide_project


def _claude_session_switch_project(name: str, project: str) -> str:
    # Проект в начале названия — своим цветом (голубовато-серым), дальше цвет плашки.
    prefix = f'{project}:'
    if not name.startswith(prefix):
        return name
    return f'\x1b[38;5;110m{prefix}\x1b[38;5;252m{name[len(prefix):]}'


def _claude_session_switch_state(color: str, state: str, question: str) -> str:
    # Колонка состояния, 8 клеток. Сессия ждёт ответа — впереди мигающий «?»: состояние
    # читают глазами по колонке (за названием знак убран — дублировал).
    if question:
        return f"{dsh_motor_badge_ask()} {color}{state:<6}"
    if state == _CLAUDE_SESSION_SWITCH_LOOK['ready'][1]:
        # «R» мигает сам терминал (SGR 5): статусная строка перерисовывается раз в секунду,
        # быстрее ею не мигнуть.
        return f"{color}\x1b[5m{state[0]}\x1b[25m{state[1:]:<7}"
    return f"{color}{state:<8}"


def _claude_session_switch_question(question: str) -> str:
    # Текст вопроса — в конце строки, оранжевым: ширина у него своя.
    if not question:
        return ''
    return f"  \x1b[38;5;208m{_claude_session_switch_short(question, CLAUDE_SESSION_SWITCH_QUESTION)}\x1b[0m"


def _claude_session_switch_cross(armed: bool) -> str:
    # «✖» закрытия (жирный — крупнее тонкого ✕): тусклый в покое, белый на красном —
    # взведён, ждёт второго клика.
    return '\x1b[1;97;41m✖\x1b[0m' if armed else '\x1b[1;38;5;242m✖\x1b[0m'


def _claude_session_switch_link(url: str, text: str, host: str = '', pid: int = 0, job: str = '',
                                cwd: str = '', account: str = '') -> str:
    # Параметры ссылки OSC 8 — на экран не выходят: `host=` — по нему окно ставит вперёд
    # сессии своей IDE; `pid=` — по нему окно узнаёт свою строку. ⚠ Не по `sessionId`:
    # после `/resume` внутри сессии Claude Code отдаёт статусной строке id нового разговора,
    # а файл сессии хранит прежний; процесс же тот же. `job=` — окно смотрит в фоновую
    # сессию: её статусную строку рисует фоновая и узнаёт свою строку по этому id.
    # `proj=` — имя папки проекта: окно ставит вперёд сессии своего проекта.
    # `acc=` — аккаунт подписки: доли чужих аккаунтов окно красит серым (окна разные).
    # Разделители `;`, `:` в значениях нельзя.
    project = os.path.basename(cwd.rstrip('/')).replace(' ', '_').replace(':', '_').replace(';', '_')
    params = ':'.join(part for part in (f"host={host.replace(' ', '_')}" if host else '',
                                         f'pid={pid}' if pid else '', f'job={job}' if job else '',
                                         f'proj={project}' if project else '',
                                         f'acc={account}' if account else '') if part)
    return f'\x1b]8;{params};{url}\x1b\\{text}\x1b]8;;\x1b\\'


def _claude_session_switch_short(text: str, limit: int) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _claude_session_switch_spent(tokens_in: int, tokens_out: int) -> str:
    # «вход/выход»: вход голубым, выход оранжевым; ширины постоянные — колонка не плывёт.
    return (f"\x1b[38;5;117m{_claude_session_switch_tokens(tokens_in):>6}\x1b[90m/"
            f"\x1b[38;5;215m{_claude_session_switch_tokens(tokens_out):<5}\x1b[0m")


def _claude_session_switch_agents(agents: list) -> str:
    # Субагенты DSH этой сессии — значками их моторов, теми же, что в строке «DSH 4/4: …»:
    # подписи там, здесь только «чьи». Последняя колонка — ширина своя. Нет — прочерк.
    if not agents:
        return '\x1b[90m–\x1b[0m'
    # Между цифрой мотора и «?» — пробел: вплотную две плашки сливаются в одну.
    return ' '.join(dsh_motor_badge(agent['motor']) + (' ' + dsh_motor_badge_ask() if agent.get('question') else '')
                    for agent in sorted(agents, key=lambda agent: str(agent['motor'])))


def _claude_session_switch_pct(pct) -> str:
    # Доля 5-часового окна подписки — жёлтым; калибровки нет — серый прочерк.
    if pct is None:
        return '\x1b[90m   –\x1b[0m'
    text = f'{pct:.0f}%' if pct >= 1 else ('<1%' if pct > 0 else '0%')
    return f'\x1b[38;5;220m{text:>4}\x1b[0m'


def _claude_session_switch_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f'{value / 1_000_000:.1f}M'
    return f'{value / 1000:.0f}k' if value >= 1000 else str(value)


def _claude_session_switch_log(line: str) -> None:
    try:
        with open(os.path.expanduser(CLAUDE_SESSION_SWITCH_LOG), 'a', encoding='utf-8') as handle:
            handle.write(f"{time.strftime('%F %T')} {line}\n")
    except OSError:
        pass


class _ClaudeSessionSwitchHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path.startswith('/switch/'):
            target = url.path[len('/switch/'):].strip('/')
            error = claude_session_switch_to(target)
        elif url.path.startswith('/close/'):
            target = 'close ' + url.path[len('/close/'):].strip('/')
            answer = claude_session_close_click(target[6:])
            error = '' if answer in ('armed', 'closed') else answer
            target += f' {answer}' if not error else ''
        elif url.path == '/backend':
            # Переключатель бэкенда справа от «+»: `pid` — процесс `claude` нажатого окна.
            query = urllib.parse.parse_qs(url.query)
            pid, backend = (query.get('pid') or ['0'])[0], (query.get('to') or [''])[0]
            target = f'backend {pid} {backend}'
            error = (claude_session_backend_request(int(pid), backend) if pid.isdigit()
                     else f'нет pid процесса claude: «{pid}»')
        elif url.path == '/new':
            query = urllib.parse.parse_qs(url.query)
            cwd, tmux_session = (query.get('cwd') or [''])[0], (query.get('tmux') or [''])[0]
            # Окно в фоновую сессию: строку рисует фоновый процесс вне tmux, `tmux=` ему не
            # узнать — сессию tmux находим по окну, которое на неё смотрит.
            tmux_session = tmux_session or _claude_session_switch_tmux_of((query.get('sid') or [''])[0])
            target = f'new {cwd}' + (f' tmux={tmux_session}' if tmux_session else '')
            error = claude_session_switch_new(cwd, tmux_session)
        else:
            self.send_response(404)
            self.end_headers()
            return
        _claude_session_switch_log(f"{target} {self.headers.get('User-Agent', '-')[:60]} "
                                   f"→ {error or 'готово'}")
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
    table.add_argument('--rows', type=int, default=CLAUDE_SESSION_SWITCH_ROWS)
    sub.add_parser('to').add_argument('session')
    args = parser.parse_args()
    if args.command == 'serve':
        claude_session_switch_serve(args.port)
    elif args.command == 'table':
        print(claude_session_switch_table(args.rows))
    else:
        reason = claude_session_switch_to(args.session)
        print(reason or 'переключено')
        sys.exit(1 if reason else 0)

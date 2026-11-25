"""Запущенные сессии Claude Code на этой машине: имя, работает или ждёт, токены, панель tmux.

Источник — файлы `~/.claude/sessions/<pid>.json`, которые каждая сессия держит сама:
`name`, `status` (`busy` — модель отвечает, `waiting` — стоит и ждёт ввода человека:
вопрос или разрешение, причина — в `waitingFor`; `shell` — ход окончен, но идёт фоновая
команда, `idle` — ждёт следующего сообщения), `kind` (`interactive` или фоновая `bg`), `sessionId`, `cwd`. Токены —
`claude_session_tokens`, панель — `tmux_pane_of_pid`.

```bash
PYTHONPATH=. python -m claude_.session.claude_session_live    # таблица в терминал
```

⚠ Файл сессии переживает её процесс (убитая сессия файл не стирает): живость — по
`/proc/<pid>`, а не по наличию файла.
"""
import glob
import json
import os

from claude_.session.claude_session_tokens import claude_session_tokens
from claude_.session.claude_session_transcript import claude_session_transcript_cwd
from claude_.session.claude_session_host import claude_session_host
from proc_.proc_ps import proc_ps_ancestors
from dsh_.dsh_board import dsh_board_motors_busy
from dsh_.dsh_saving import dsh_saving_calibrate, dsh_saving_owner_of
from dsh_.dsh_session import dsh_session_asking
from dsh_.dsh_wait import dsh_wait_owner_of
from tmux_.tmux_ import tmux_clients, tmux_pane_of_pid, tmux_panes

# Где сессии держат свои файлы.
CLAUDE_SESSION_LIVE_DIR = '~/.claude/sessions'

# Порядок статусов в списке: ждёт ответа, работает, фоновая команда, ждёт сообщения.
CLAUDE_SESSION_LIVE_ORDER = ('waiting', 'busy', 'shell', 'idle')


def claude_session_live_list() -> list:
    """Живые сессии Claude Code: работающие, затем с фоновой командой, затем ждущие; свежие сверху.

    Returns:
        `[{sessionId, pid, name, title, status, kind, cwd, updated, tokens_in, tokens_out, window_pct, agents, question, job, pane, visible}]`;
        `title` — как в шапке вкладки: у сессии в tmux — заголовок её панели (его ставит
        сам Claude Code), иначе — из транскрипта; пусто — заголовка ещё нет.
        ⚠ Заголовок панели верен и после `/resume` внутри сессии: файл сессии хранит прежний
        `sessionId`, и транскрипт по нему — пустой новый, без заголовка;
        `window_pct` — сколько процентов 5-часового окна подписки обошлась сессия (как
        процент экономии субагентов: взвешенные токены / цена 1% окна её аккаунта);
        `None` — калибровки ещё нет;
        `agents` — субагенты DSH, работающие сейчас по заданиям этой сессии:
        `[{motor, name, question}]` (мотор и подпись — как в строке «DSH 4/4: …»;
        `question` — субагент стоит и ждёт ответа человека); хост DSH не
        отвечает — пусто у всех;
        `question` — сессия стоит и ждёт ввода (`status: waiting`): причина из
        `waitingFor` («input needed»); нет — пусто;
        `job` — окно смотрит в фоновую сессию (`/resume` фоновой изнутри сессии): её
        `sessionId`; строка окна тогда несёт название, состояние, токены и субагентов
        фоновой (`_claude_session_live_viewers`); нет — пусто;
        `host` — где сессия видна: «PyCharm», «PhpStorm», … (`claude_session_host`);
        `pane` — цель tmux (`session:window.pane`) или пусто, если сессия не в tmux;
        `visible` — у сессии есть терминал на экране: не фоновая (`bg` терминала не имеет),
        не в отсоединённом tmux (процесс жив, но смотреть его неоткуда) и не вложенный
        запуск (`_claude_session_live_nested`).
    """
    rows = []
    for path in glob.glob(os.path.join(os.path.expanduser(CLAUDE_SESSION_LIVE_DIR), '*.json')):
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        pid = data.get('pid')
        if not isinstance(pid, int) or not os.path.exists(f'/proc/{pid}') or not data.get('sessionId'):
            continue
        status = data.get('status') or '?'
        rows.append({'sessionId': data['sessionId'], 'pid': pid,
                     'name': data.get('name') or data['sessionId'][:8], 'status': status,
                     'kind': data.get('kind') or '', 'entrypoint': data.get('entrypoint') or '',
                     # ⚠ Вопрос виден только здесь: в транскрипт строку вопроса Claude Code
                     # дописывает вместе с ответом, пока она висит — её в файле нет.
                     'question': (data.get('waitingFor') or 'вопрос') if status == 'waiting' else '',
                     'cwd': data.get('cwd') or '',
                     'updated': data.get('statusUpdatedAt') or data.get('updatedAt') or 0})
    panes = tmux_panes()
    digest = claude_session_tokens(rows)
    attached = {pane['target'] for pane in panes if pane['attached']}
    pids = {row['pid'] for row in rows}
    agents = _claude_session_live_agents()
    clients = tmux_clients() if panes else []
    for row in rows:
        row['tokens_in'] = digest.get(row['sessionId'], {}).get('tokens_in', 0)
        row['tokens_out'] = digest.get(row['sessionId'], {}).get('tokens_out', 0)
        row['agents'] = agents.get(row['sessionId'], [])
        row['window_pct'] = _claude_session_live_pct(row['sessionId'], digest.get(row['sessionId'], {}))
        row['pane'] = tmux_pane_of_pid(row['pid'], panes) if panes else ''
        row['title'] = (_claude_session_live_pane_title(row['pane'], panes)
                        or digest.get(row['sessionId'], {}).get('title', ''))
        row['visible'] = (row['kind'] != 'bg' and (not row['pane'] or row['pane'] in attached)
                          and not _claude_session_live_nested(row, pids))
        tmux_session = row['pane'].split(':', 1)[0] if row['pane'] else ''
        row['host'] = claude_session_host(row['pid'], tmux_session, clients) if row['visible'] else ''
    _claude_session_live_viewers(rows)
    rank = {status: index for index, status in enumerate(CLAUDE_SESSION_LIVE_ORDER)}
    rows.sort(key=lambda row: (rank.get(row['status'], len(rank)), -row['updated']))
    return rows


def claude_session_live_orphans(rows: list) -> list:
    """Субагенты DSH, чей хозяин не виден в таблице: сессия закрыта или скрыта (фоновая,
    в отсоединённом tmux). Их моторы иначе пропали бы из таблицы, хотя работают.

    Args:
        rows: `claude_session_live_list()` целиком — по нему видно, жив ли хозяин.

    Returns:
        `[{owner, title, project, state, agents}]`: `state` — «скрыта» (процесс жив, но
        строки у него нет) или «закрыта»; `project` — проект хозяина (по его транскрипту).
        ⚠ Не папка карточки субагента: она — его рабочая (`scratchpad/…/t4`, копия репо).
    """
    shown = {row['sessionId'] for row in rows if row['visible']} | {row['job'] for row in rows if row['visible']}
    alive = {row['sessionId']: row for row in rows}
    orphans = {}
    for owner, agents in _claude_session_live_agents().items():
        if owner in shown:
            continue
        cwd = alive[owner]['cwd'] if owner in alive else claude_session_transcript_cwd(owner)
        orphans[owner] = {'owner': owner, 'agents': agents, 'state': 'скрыта' if owner in alive else 'закрыта',
                          'project': os.path.basename(cwd.rstrip('/'))}
    titles = claude_session_tokens([{'sessionId': owner, 'cwd': ''} for owner in orphans])
    for owner, item in orphans.items():
        item['title'] = titles.get(owner, {}).get('title', '')
    return list(orphans.values())


def claude_session_live_find(session_id: str) -> dict:
    """Живая сессия по `sessionId` (или его началу), с заголовком; нет — пустой словарь."""
    for row in claude_session_live_list():
        if session_id and row['sessionId'].startswith(session_id):
            return row
    return {}


def _claude_session_live_pct(session_id: str, digest: dict):
    # Калибровка — по аккаунту самой сессии (снимок её `rate_limits`): на машине бывают
    # сессии разных аккаунтов, и цена процента у них своя. Кэш калибровки — 5 минут.
    per_pct = (dsh_saving_calibrate(session_id).get('five_hour') or {}).get('per_pct')
    if not per_pct or not digest.get('units'):
        return None
    return digest['units'] / per_pct


def _claude_session_live_agents() -> dict:
    # Занятые моторы DSH и чья это работа: хозяин карточки — сессия Claude, что её завела.
    # Хост лёг или медлит — колонка пустеет, таблица из-за него не ломается.
    try:
        working = dsh_board_motors_busy()['working']
        asking = dsh_session_asking([item.get('session') or '' for item in working])
    except Exception:
        return {}
    agents = {}
    for item in working:
        # Хозяин — кто сейчас ждёт ответа (последним дал задание), иначе — кто завёл карточку.
        owner = dsh_wait_owner_of(item.get('session') or '') or dsh_saving_owner_of(item.get('session') or '')
        if owner:
            agents.setdefault(owner, []).append({
                'motor': item.get('motor'),
                'question': asking.get(item.get('session') or '', ''),
                'name': item.get('goal') or item.get('title') or item.get('session')})
    return agents


def _claude_session_live_pane_title(pane: str, panes: list) -> str:
    # Заголовок панели без значков Claude Code в начале (✳, крутилка) и служебного
    # «Claude Code» — его ставит свежая сессия, пока своего заголовка нет.
    title = next((row['title'] for row in panes if row['target'] == pane), '') if pane else ''
    title = title.lstrip(' ✳·✻✽✶✢*⠁⠂⠄⡀⢀⠠⠐⠈◐◑◒◓').strip()
    return '' if title in ('Claude Code', 'claude') else title


def _claude_session_live_viewers(rows: list) -> None:
    # Окно, смотрящее в фоновую сессию: Claude Code в нём держит свою пустую сессию, а
    # работу ведёт фоновая (скрыта — терминала у неё нет), и статусную строку окну рисует
    # она. Окно подключено к управляющему процессу фоновых сессий, а в какую именно оно
    # смотрит, по сокету не видно — связь по заголовку: шапку панели окна и заголовок
    # фоновой ставит сам Claude Code. Строка окна берёт данные фоновой.
    jobs = {row['title']: row for row in rows if row['kind'] == 'bg' and row['title']}
    for row in rows:
        row.setdefault('job', '')
        job = jobs.get(row['title']) if row['kind'] != 'bg' and row['pane'] else None
        if not job:
            continue
        row['job'] = job['sessionId']
        for key in ('status', 'question', 'tokens_in', 'tokens_out', 'window_pct', 'agents', 'updated'):
            row[key] = job[key]


def _claude_session_live_nested(row: dict, pids: set) -> bool:
    # Вложенный запуск — не сессия человека: Agent SDK (`entrypoint: sdk-py` — ревьюер
    # заставы и т. п.) или процесс-потомок другой живой сессии. Иначе его панель находится
    # по предкам — панель хозяина, и с ней заголовок: в таблице выходил двойник.
    if row['entrypoint'].startswith('sdk'):
        return True
    return any(ancestor in pids for ancestor in proc_ps_ancestors(row['pid']))

if __name__ == '__main__':
    for item in claude_session_live_list():
        print(f"{'●' if item['status'] == 'busy' else '○'} {item['name'][:34]:<34} {item['status']:<6} "
              f"{item['tokens_in']:>10,} / {item['tokens_out']:<8,} {item['pane'] or '—'}")

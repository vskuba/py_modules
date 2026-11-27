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
import threading
import time

from claude_.session.claude_session_tokens import claude_session_tokens, claude_session_tokens_since
from claude_.session.claude_session_transcript import claude_session_transcript_cwd
from claude_.session.claude_session_host import claude_session_host
from claude_.session.claude_session_name import claude_session_name_get
from claude_.session.claude_session_ready import claude_session_ready_mark
from proc_.proc_ps import proc_ps_ancestors
from dsh_.dsh_board import dsh_board_motors_busy
from dsh_.dsh_saving import dsh_saving_calibrate, dsh_saving_owner_of
from dsh_.dsh_session import dsh_session_asking
from dsh_.dsh_wait import dsh_wait_owner_of
from tmux_.tmux_ import tmux_clients, tmux_pane_label, tmux_pane_of_pid, tmux_panes

# Где сессии держат свои файлы.
CLAUDE_SESSION_LIVE_DIR = '~/.claude/sessions'

# Сбросы недельного окна ближе этого — один аккаунт (Anthropic сдвигает сброс на час-другой).
# ⚠ Суточный допуск склеил два аккаунта со сбросами в 9 ч друг от друга (окна 48% и 96%);
# у калибровки субагентов (`DSH_SAVING_ACCOUNT_DRIFT`) теперь те же 4 ч.
CLAUDE_SESSION_LIVE_ACCOUNT_DRIFT = 4 * 3600

# Порядок статусов в списке: ждёт ответа, работает, фоновая команда, ждёт сообщения.
CLAUDE_SESSION_LIVE_ORDER = ('waiting', 'busy', 'shell', 'idle')
# Колонка субагентов DSH: сколько секунд ждать хост. Таблицу обрывает `timeout 8` статусной
# строки, а у запросов к хосту свои 10 с каждый: медленный хост останавливал всю таблицу
# (и с ней новое название `t …`). Не успел — берётся прошлый удачный ответ из кэша.
CLAUDE_SESSION_LIVE_AGENTS_WAIT = 3.0
CLAUDE_SESSION_LIVE_AGENTS_CACHE = '~/.cache/claude-session-agents.json'
# Прошлый ответ старше этого — уже не правда: колонка пустеет.
CLAUDE_SESSION_LIVE_AGENTS_STALE = 120


def claude_session_live_list() -> list:
    """Живые сессии Claude Code: работающие, затем с фоновой командой, затем ждущие; свежие сверху.

    Returns:
        `[{sessionId, pid, name, title, status, kind, cwd, updated, tokens_in, tokens_out, window_in, window_out, window_pct, agents, question, job, pane, visible}]`;
        `title` — своё название (`t …`, `claude_session_name`), иначе как в шапке вкладки: у сессии в tmux — заголовок её панели (его ставит
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
        `ready` — отработала, пока на неё не смотрели (`claude_session_ready`);
        `account` — аккаунт подписки (метка по сбросу недельного окна; у окна в фоновую —
        аккаунт фоновой): у разных аккаунтов разные окна, их доли не складываются;
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
        # Вход/выход в текущем 5-часовом окне — рядом с «всего за сессию».
        start = _claude_session_live_window_start(row['sessionId'])
        found = digest.get(row['sessionId'], {})
        row['window_in'] = int(claude_session_tokens_since(found.get('buckets_in'), start)) if start else 0
        row['window_out'] = int(claude_session_tokens_since(found.get('buckets_out'), start)) if start else 0
        row['pane'] = tmux_pane_of_pid(row['pid'], panes) if panes else ''
        row['title'] = (_claude_session_live_pane_title(row['pane'], panes)
                        or digest.get(row['sessionId'], {}).get('title', ''))
        nested = _claude_session_live_nested(row, pids)
        row['visible'] = (row['kind'] != 'bg' and (not row['pane'] or row['pane'] in attached) and not nested)
        # Скрыта — жива в tmux, но ни одна вкладка её не показывает (вкладку закрыли, IDE
        # перезапустили): клик по ней в таблице выводит её своей вкладкой.
        row['hidden'] = bool(row['kind'] != 'bg' and row['pane'] and row['pane'] not in attached and not nested)
        tmux_session = row['pane'].split(':', 1)[0] if row['pane'] else ''
        row['host'] = claude_session_host(row['pid'], tmux_session, clients) if row['visible'] else ''
    _claude_session_live_viewers(rows)
    _claude_session_live_accounts(rows)
    for row in rows:
        # Своё название (`t …` в сессии) — сильнее заголовка, что дал Claude Code.
        custom = claude_session_name_get(row['sessionId'], f"pid:{row['pid']}", row['job'])
        row['title'] = custom or row['title']
        # Шапка вкладки терминала — тоже своим названием: её собирает tmux из опции панели.
        label = next((pane['label'] for pane in panes if pane['target'] == row['pane']), '') if row['pane'] else ''
        if row['pane'] and label != custom:
            tmux_pane_label(row['pane'], custom)
    claude_session_ready_mark(rows, {pane['target'] for pane in panes if pane['shown']})
    rank = {status: index for index, status in enumerate(CLAUDE_SESSION_LIVE_ORDER)}
    rows.sort(key=lambda row: (rank.get(row['status'], len(rank)), -row['updated']))
    return rows


def claude_session_live_orphans(rows: list) -> list:
    """Сессии, которых не видно в таблице, но которые живы или работают: скрытые (в tmux без
    вкладки) и хозяева субагентов DSH, закрытые или скрытые. Иначе после перезапуска IDE
    их не находили и открывали те же беседы заново `/resume` — выходили двойники.

    Args:
        rows: `claude_session_live_list()` целиком — по нему видно, жив ли хозяин.

    Returns:
        `[{owner, pid, copies, title, project, state, agents}]`: `state` — «скрыта» (процесс
        жив, но вкладки нет) или «закрыта»; `pid` — процесс скрытой копии, по нему клик
        выводит именно её (у двойника тот же `sessionId`, что у видимой); `copies` — сколько
        скрытых копий у беседы (окна в одну фоновую — тоже копии). `project` — проект хозяина
        (по его транскрипту). ⚠ Не папка карточки субагента: она — его рабочая
        (`scratchpad/…/t4`, копия репо). Скрытая без заголовка (пустая, ни одного
        сообщения) — не строка: таких копятся десятки.
    """
    shown = {row['sessionId'] for row in rows if row['visible']} | {row['job'] for row in rows if row['visible']}
    alive = {row['sessionId']: row for row in rows}
    orphans = {}
    for row in rows:
        key = row['job'] or row['sessionId']
        if not row.get('hidden') or not row['title'] or key in shown:
            continue
        if key in orphans:
            orphans[key]['copies'] += 1
            continue
        orphans[key] = {'owner': key, 'pid': row['pid'], 'copies': 1, 'agents': [], 'state': 'скрыта',
                        'project': os.path.basename(row['cwd'].rstrip('/')), 'title': row['title']}
    for owner, agents in _claude_session_live_agents().items():
        if owner in shown:
            continue
        if owner in orphans:
            orphans[owner]['agents'] = agents
            continue
        cwd = alive[owner]['cwd'] if owner in alive else claude_session_transcript_cwd(owner)
        orphans[owner] = {'owner': owner, 'pid': 0, 'copies': 0, 'agents': agents,
                          'state': 'скрыта' if owner in alive else 'закрыта',
                          'project': os.path.basename(cwd.rstrip('/')), 'title': ''}
    titles = claude_session_tokens([{'sessionId': owner, 'cwd': ''} for owner, item in orphans.items()
                                    if not item['title']])
    # Название — то же, что было в строке хозяина: своё (`t …`) сильнее заголовка транскрипта,
    # иначе скрытая сессия внизу выходила под другим именем и казалась новой.
    for owner, item in orphans.items():
        item['title'] = (item['title'] or (alive[owner]['title'] if owner in alive else claude_session_name_get(owner))
                         or titles.get(owner, {}).get('title', ''))
    return sorted(orphans.values(), key=lambda item: (item['project'].lower(), item['title'].lower()))


def claude_session_live_find(session_id: str) -> dict:
    """Живая сессия по `sessionId` (или его началу), с заголовком; нет — пустой словарь.

    ⚠ Одну беседу бывает открывают двумя процессами (`/resume` уже открытой в другом окне).
    Берётся копия, видная на экране: невидимая уводила под себя чужую вкладку.
    """
    rows = [row for row in claude_session_live_list() if session_id and row['sessionId'].startswith(session_id)]
    rows.sort(key=lambda row: not row['visible'])
    return rows[0] if rows else {}


def _claude_session_live_pct(session_id: str, digest: dict):
    # Калибровка — по аккаунту самой сессии (снимок её `rate_limits`): на машине бывают
    # сессии разных аккаунтов, и цена процента у них своя. Кэш калибровки — 5 минут.
    # ⚠ Только расход в **текущем** 5-часовом окне (с `resets_at` − 5 ч, по корзинам сводки):
    # за всю жизнь долгая сессия набирала сотни процентов — цену многих окон и аккаунтов.
    per_pct = (dsh_saving_calibrate(session_id).get('five_hour') or {}).get('per_pct')
    start = _claude_session_live_window_start(session_id)
    if not per_pct or not start:
        return None
    units = claude_session_tokens_since(digest.get('buckets'), start)
    return units / per_pct if units else None


def _claude_session_live_window_start(session_id: str) -> float:
    # Начало текущего 5-часового окна — из снимка `rate_limits`, который статусная строка
    # кладёт на сессию; своего снимка нет — свежайший (окно у аккаунта общее).
    snapshots = []
    for path in glob.glob(os.path.expanduser('~/.cache/claude-rate/*.json')):
        try:
            with open(path, encoding='utf-8') as handle:
                snapshots.append(json.load(handle))
        except (OSError, ValueError):
            continue
    mine = [row for row in snapshots if row.get('session_id') == session_id]
    pool = mine or sorted(snapshots, key=lambda row: -float(row.get('at') or 0))[:1]
    resets = (((pool[0].get('rate_limits') or {}).get('five_hour') or {}).get('resets_at')) if pool else None
    return float(resets) - 5 * 3600 if resets else 0.0


_CLAUDE_SESSION_LIVE_AGENTS = {}


def _claude_session_live_agents() -> dict:
    # Субагенты DSH с потолком по времени: опрос хоста — в фоновой нити, которую выход
    # процесса не ждёт. Один опрос на прогон: список и «скрытые» зовут его оба.
    if 'value' in _CLAUDE_SESSION_LIVE_AGENTS:
        return _CLAUDE_SESSION_LIVE_AGENTS['value']
    result = {}
    thread = threading.Thread(target=lambda: result.update(value=_claude_session_live_agents_fetch()),
                              daemon=True)
    thread.start()
    thread.join(CLAUDE_SESSION_LIVE_AGENTS_WAIT)
    path = os.path.expanduser(CLAUDE_SESSION_LIVE_AGENTS_CACHE)
    if result.get('value') is not None:
        value = result['value']
        try:
            with open(f'{path}.tmp', 'w', encoding='utf-8') as handle:
                json.dump({'at': time.time(), 'agents': value}, handle, ensure_ascii=False)
            os.replace(f'{path}.tmp', path)
        except OSError:
            pass
    else:
        try:
            with open(path, encoding='utf-8') as handle:
                cached = json.load(handle)
            fresh = time.time() - cached.get('at', 0) < CLAUDE_SESSION_LIVE_AGENTS_STALE
            value = cached.get('agents') or {} if fresh else {}
        except (OSError, ValueError, AttributeError):
            value = {}
    _CLAUDE_SESSION_LIVE_AGENTS['value'] = value
    return value


def _claude_session_live_agents_fetch():
    # Занятые моторы DSH и чья это работа: хозяин карточки — сессия Claude, что её завела.
    # Хост лёг — None (не «субагентов нет»): тогда берётся кэш, таблица из-за него не ломается.
    try:
        working = dsh_board_motors_busy()['working']
        asking = dsh_session_asking([item.get('session') or '' for item in working])
    except Exception:
        return None
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
        for key in ('status', 'question', 'tokens_in', 'tokens_out', 'window_in', 'window_out', 'window_pct',
                    'agents', 'updated'):
            row[key] = job[key]


def _claude_session_live_nested(row: dict, pids: set) -> bool:
    # Вложенный запуск — не сессия человека: Agent SDK (`entrypoint: sdk-py` — ревьюер
    # заставы и т. п.) или процесс-потомок другой живой сессии. Иначе его панель находится
    # по предкам — панель хозяина, и с ней заголовок: в таблице выходил двойник.
    if row['entrypoint'].startswith('sdk'):
        return True
    return any(ancestor in pids for ancestor in proc_ps_ancestors(row['pid']))


def _claude_session_live_accounts(rows: list) -> None:
    # Аккаунт — по сбросу недельного окна в снимке `rate_limits` сессии (у окна в фоновую —
    # фоновой). Anthropic сдвигает сброс у того же аккаунта на час-другой: близкие значения
    # (`CLAUDE_SESSION_LIVE_ACCOUNT_DRIFT`) — один аккаунт, метка — наименьшее из них.
    resets = {}
    for row in rows:
        resets[row['sessionId']] = _claude_session_live_reset(row['job'] or row['sessionId'])
    labels = sorted({value for value in resets.values() if value})
    canon = {}
    for value in labels:
        canon[value] = next((seen for seen in canon.values()
                             if abs(float(seen) - float(value)) < CLAUDE_SESSION_LIVE_ACCOUNT_DRIFT), value)
    for row in rows:
        row['account'] = canon.get(resets[row['sessionId']], '')


def _claude_session_live_reset(session_id: str) -> str:
    try:
        with open(os.path.expanduser(f'~/.cache/claude-rate/{session_id}.json'), encoding='utf-8') as handle:
            limits = json.load(handle).get('rate_limits') or {}
    except (OSError, ValueError):
        return ''
    return str((limits.get('seven_day') or {}).get('resets_at') or '')

if __name__ == '__main__':
    for item in claude_session_live_list():
        print(f"{'●' if item['status'] == 'busy' else '○'} {item['name'][:34]:<34} {item['status']:<6} "
              f"{item['tokens_in']:>10,} / {item['tokens_out']:<8,} {item['pane'] or '—'}")

"""Запущенные сессии Claude Code на этой машине: имя, работает или ждёт, токены, панель tmux.

Источник — файлы `~/.claude/sessions/<pid>.json`, которые каждая сессия держит сама:
`name`, `status` (`busy` — модель отвечает, `shell` — ход окончен, но идёт фоновая
команда, `idle` — ждёт человека), `kind` (`interactive` или фоновая `bg`), `sessionId`, `cwd`. Токены —
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
from claude_.session.claude_session_host import claude_session_host
from dsh_.dsh_board import dsh_board_motors_busy
from dsh_.dsh_saving import dsh_saving_calibrate, dsh_saving_owner_of
from tmux_.tmux_ import tmux_clients, tmux_pane_of_pid, tmux_panes

# Где сессии держат свои файлы.
CLAUDE_SESSION_LIVE_DIR = '~/.claude/sessions'

# Порядок статусов в списке: работает, фоновая команда, ждёт; незнакомый — в конце.
CLAUDE_SESSION_LIVE_ORDER = ('busy', 'shell', 'idle')


def claude_session_live_list() -> list:
    """Живые сессии Claude Code: работающие, затем с фоновой командой, затем ждущие; свежие сверху.

    Returns:
        `[{sessionId, pid, name, title, status, kind, cwd, updated, tokens_in, tokens_out, window_pct, agents, question, pane, visible}]`;
        `title` — заголовок из транскрипта (шапка вкладки), пусто — его ещё нет;
        `window_pct` — сколько процентов 5-часового окна подписки обошлась сессия (как
        процент экономии субагентов: взвешенные токены / цена 1% окна её аккаунта);
        `None` — калибровки ещё нет;
        `agents` — субагенты DSH, работающие сейчас по заданиям этой сессии:
        `[{motor, name}]` (мотор и подпись — как в строке «DSH 4/4: …»); хост DSH не
        отвечает — пусто у всех;
        `question` — вопрос `AskUserQuestion`, который ждёт ответа человека; нет — пусто;
        `host` — где сессия видна: «PyCharm», «PhpStorm», … (`claude_session_host`);
        `pane` — цель tmux (`session:window.pane`) или пусто, если сессия не в tmux;
        `visible` — у сессии есть терминал на экране: не фоновая (`bg` терминала не имеет)
        и не в отсоединённом tmux (процесс жив, но смотреть его неоткуда).
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
                     'kind': data.get('kind') or '',
                     'cwd': data.get('cwd') or '',
                     'updated': data.get('statusUpdatedAt') or data.get('updatedAt') or 0})
    panes = tmux_panes()
    digest = claude_session_tokens(rows)
    attached = {pane['target'] for pane in panes if pane['attached']}
    agents = _claude_session_live_agents()
    clients = tmux_clients() if panes else []
    for row in rows:
        row['tokens_in'] = digest.get(row['sessionId'], {}).get('tokens_in', 0)
        row['tokens_out'] = digest.get(row['sessionId'], {}).get('tokens_out', 0)
        row['question'] = digest.get(row['sessionId'], {}).get('question', '')
        row['agents'] = agents.get(row['sessionId'], [])
        row['window_pct'] = _claude_session_live_pct(row['sessionId'], digest.get(row['sessionId'], {}))
        row['title'] = digest.get(row['sessionId'], {}).get('title', '')
        row['pane'] = tmux_pane_of_pid(row['pid'], panes) if panes else ''
        row['visible'] = row['kind'] != 'bg' and (not row['pane'] or row['pane'] in attached)
        tmux_session = row['pane'].split(':', 1)[0] if row['pane'] else ''
        row['host'] = claude_session_host(row['pid'], tmux_session, clients) if row['visible'] else ''
    rank = {status: index for index, status in enumerate(CLAUDE_SESSION_LIVE_ORDER)}
    rows.sort(key=lambda row: (rank.get(row['status'], len(rank)), -row['updated']))
    return rows


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
    except Exception:
        return {}
    agents = {}
    for item in working:
        owner = dsh_saving_owner_of(item.get('session') or '')
        if owner:
            agents.setdefault(owner, []).append({
                'motor': item.get('motor'),
                'name': item.get('goal') or item.get('title') or item.get('session')})
    return agents

if __name__ == '__main__':
    for item in claude_session_live_list():
        print(f"{'●' if item['status'] == 'busy' else '○'} {item['name'][:34]:<34} {item['status']:<6} "
              f"{item['tokens_in']:>10,} / {item['tokens_out']:<8,} {item['pane'] or '—'}")

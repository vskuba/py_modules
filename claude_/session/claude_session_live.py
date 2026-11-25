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
from tmux_.tmux_ import tmux_pane_of_pid, tmux_panes

# Где сессии держат свои файлы.
CLAUDE_SESSION_LIVE_DIR = '~/.claude/sessions'

# Порядок статусов в списке: работает, фоновая команда, ждёт; незнакомый — в конце.
CLAUDE_SESSION_LIVE_ORDER = ('busy', 'shell', 'idle')


def claude_session_live_list() -> list:
    """Живые сессии Claude Code: работающие, затем с фоновой командой, затем ждущие; свежие сверху.

    Returns:
        `[{sessionId, pid, name, title, status, kind, cwd, updated, tokens, pane, visible}]`;
        `title` — заголовок из транскрипта (шапка вкладки), пусто — его ещё нет;
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
    for row in rows:
        row['tokens'] = digest.get(row['sessionId'], {}).get('tokens', 0)
        row['title'] = digest.get(row['sessionId'], {}).get('title', '')
        row['pane'] = tmux_pane_of_pid(row['pid'], panes) if panes else ''
        row['visible'] = row['kind'] != 'bg' and (not row['pane'] or row['pane'] in attached)
    rank = {status: index for index, status in enumerate(CLAUDE_SESSION_LIVE_ORDER)}
    rows.sort(key=lambda row: (rank.get(row['status'], len(rank)), -row['updated']))
    return rows


def claude_session_live_find(session_id: str) -> dict:
    """Живая сессия по `sessionId` (или его началу), с заголовком; нет — пустой словарь."""
    for row in claude_session_live_list():
        if session_id and row['sessionId'].startswith(session_id):
            return row
    return {}


if __name__ == '__main__':
    for item in claude_session_live_list():
        print(f"{'●' if item['status'] == 'busy' else '○'} {item['name'][:34]:<34} {item['status']:<6} "
              f"{item['tokens']:>10,}  {item['pane'] or '—'}")

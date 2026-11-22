"""Журнал раздачи `fanout` на диске: оборванный оркестратор дособирает ответы, а не теряет.

`dsh-board fanout tasks.json --out-dir D` ведёт в `D/fanout.json` строку на задачу: карточку
и копию репо — сразу, как задача их получила, ответ — в `D/<n>.md` сразу, как задача кончилась.
Если сам `fanout` убили (перезапуск, таймаут, рестарт оркестратора), карточки на хосте
доработают — а `dsh-board collect D` дождётся их и допишет недостающие ответы.

```bash
dsh-board fanout tasks.json --out-dir out/      # пишет out/fanout.json и out/1.md, 2.md …
dsh-board collect out/                          # дособрать то, что fanout не дописал
```

⚠ Ожидание в `collect` — по лёгкому маршруту `working` (кто занят), журнал карточки
читается один раз, в конце: тяжёлые вызовы в цикле однажды уже уложили хост.
"""
import contextlib
import json
import os
import threading
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DshApiError
from dsh_.dsh_wait import dsh_wait_done

DSH_FANOUT_LOG_FILE = 'fanout.json'
DSH_FANOUT_LOG_POLL = 20.0

_DSH_FANOUT_LOG_LOCK = threading.Lock()


def dsh_fanout_log_start(out_dir: str, index: int, row: dict, title: str = '') -> None:
    """Задача номер `index` (с нуля) получила карточку — записать до того, как ждать ответ."""
    _update(out_dir, index, {**row, 'title': title, 'state': 'running'})


def dsh_fanout_log_done(out_dir: str, index: int, row: dict) -> str:
    """Задача кончилась: ответ — в `<n>.md`, строка журнала — `done`/`error`.

    Returns:
        Путь к файлу ответа.
    """
    path = os.path.join(out_dir, f'{index + 1}.md')
    _write_text(path, row.get('answer') or row.get('error') or '')
    _update(out_dir, index, {key: value for key, value in row.items() if key != 'answer'}
            | {'state': 'error' if row.get('error') else 'done', 'out': path})
    return path


def dsh_fanout_log_read(out_dir: str) -> list:
    """Строки журнала раздачи по порядку задач; нет журнала — пустой список."""
    try:
        with open(os.path.join(out_dir, DSH_FANOUT_LOG_FILE), encoding='utf-8') as handle:
            return json.load(handle).get('tasks') or []
    except (OSError, ValueError):
        return []


def dsh_fanout_log_collect(out_dir: str, timeout: float = 3600, *,
                           base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Дособрать раздачу: дождаться недописанных карточек и записать их ответы.

    Returns:
        Строки журнала после сбора; у недождавшихся `state` остаётся `running`.
    """
    from dsh_.dsh_board import dsh_board_inbox_take, dsh_board_turns, dsh_board_working  # ленивый: dsh_board зовёт этот модуль
    from dsh_.dsh_session import dsh_session_answer_last, dsh_session_find
    deadline = time.time() + timeout
    while True:
        rows = dsh_fanout_log_read(out_dir)
        pending = {row.get('session') for row in rows if row.get('state') == 'running' and row.get('session')}
        try:
            busy = {row['session'] for row in dsh_board_working(base=base)}
        except DshApiError:
            busy = pending
        for index, row in enumerate(rows):
            session = row.get('session')
            if row.get('state') == 'running' and session and session not in busy:
                try:
                    answer, error = dsh_session_answer_last(session, base=base) or '', ''
                except DshApiError as failure:
                    answer, error = '', f'{type(failure).__name__}: {failure}'
                dsh_fanout_log_done(out_dir, index, {**row, 'answer': answer, 'error': error})
                if answer:
                    # Ответ забран в файл — во «входящих» его больше нет, как после `ask`.
                    with contextlib.suppress(DshApiError, OSError):
                        dsh_board_inbox_take(session, dsh_board_turns(dsh_session_find(session, base=base)))
                        dsh_wait_done(session)
        if not (pending & busy) or time.time() >= deadline:
            return dsh_fanout_log_read(out_dir)
        time.sleep(DSH_FANOUT_LOG_POLL)


def _update(out_dir: str, index: int, patch: dict) -> None:
    with _DSH_FANOUT_LOG_LOCK:
        os.makedirs(out_dir, exist_ok=True)
        rows = dsh_fanout_log_read(out_dir)
        rows += [{} for _ in range(index + 1 - len(rows))]
        rows[index] = {**rows[index], **patch}
        path = os.path.join(out_dir, DSH_FANOUT_LOG_FILE)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump({'tasks': rows}, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', errors='replace') as handle:
        handle.write(text)

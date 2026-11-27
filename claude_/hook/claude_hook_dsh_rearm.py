"""
Перезапуск харнеса не теряет ответы субагентов: хук будит оркестратора и ставит ждун на место.

Хук Claude Code на `SessionStart` (и безопасен на `UserPromptSubmit`). Фоновый ждун
`dsh-board wait` живёт, пока жив его процесс харнеса, и перезапуск его убивает; разговор
при этом продолжается тем же `CLAUDE_CODE_SESSION_ID`, но ждун уже никто не поднимает,
и ответы, легшие за это время, лежат неразобранными часами (2026-11-26: три таких мёртвых
окна, самое длинное — 189 минут). Застава на `Stop` (`claude_hook_dsh_wait`) тут бессильна:
ход уже закрыт был не ею.

Хук смотрит три вещи про текущую сессию:

* ответы, которые ждун **успел собрать** в `<TMPDIR>/dsh-wait-<сессия>/` и которые
  сессия ещё не видела (виденное запоминается в `~/.dsh/dsh-board.rearm-seen.json`;
  файл тот же перезапишут новым mtime — ответ считается новым);
* карточки, чей ответ лежит во «входящих», но метка `dsh_wait` всё ещё стоит
  (`dsh_wait_answered` — тот же признак, что у ждуна и заставы живости) — их ждун заберёт,
  когда его перезаведут;
* метки без живого ждуна (`dsh_wait_waiter_alive`) — субагенты ещё работают, будить
  некому.

Ничего из этого хук сам не запускает и не отменяет: он кладёт модели `additionalContext`
с конкретикой — что готово и где лежит, что ещё идёт, и готовую команду ждуна
(`cd /tmp && dsh-board wait --out-dir <каталог>` фоном). Нечего сказать — молчит.

    echo '{"session_id":"s","hook_event_name":"SessionStart"}' \\
      | python3 claude_/hook/claude_hook_dsh_rearm.py

⚠ Хук при своей поломке молчит и пишет в `stderr` — старту сессии он не враг ни при
каком раскладе (тот же договор, что у `claude_hook_stop_run`).

⚠ Тяжёлых вызовов хосту — ровно один (`dsh_wait_answered`) и только когда метки есть,
а ждуна нет; и тот под потолком `CLAUDE_HOOK_DSH_REARM_HOST_WAIT` на отдельной нити:
засохший кэш `session/list` не имеет права задержать старт сессии дольше секунды. Локальные
проверки (`dsh_wait_pending`, `dsh_wait_waiter_alive`, обход `<TMPDIR>/dsh-wait-*`) — те же,
что делает `claude_hook_dsh_wait`.

⚠ `_state_write` — last-writer-wins: два хука разных сессий пишут непересекающиеся
наборы путей (у каждой сессии свой каталог ждуна), потеря записи максимум удваивает
одно напоминание.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from claude_.hook.claude_hook import claude_hook_input  # noqa: E402
from dsh_.dsh_api import DSH_API_HOME_DEFAULT  # noqa: E402
from dsh_.dsh_wait import (DSH_WAIT_MAX_AGE, DSH_WAIT_OWNER_ENV, dsh_wait_answered,  # noqa: E402
                           dsh_wait_key, dsh_wait_pending, dsh_wait_waiter_alive)

# Виденные ответы: {файл: mtime} — чтобы тот же файл не всплывал на каждом ходе.
CLAUDE_HOOK_DSH_REARM_STATE = 'dsh-board.rearm-seen.json'

# Сколько путей перечислять в строке; остальные — числом («и ещё N в каталоге»).
CLAUDE_HOOK_DSH_REARM_SHOWN = 5

# Потолок ожидания «входящих»: не успел хост — отчёт без них, но в срок.
CLAUDE_HOOK_DSH_REARM_HOST_WAIT = 0.8

CLAUDE_HOOK_DSH_REARM_HEAD_DEAD = ('DSH: {count} subagent answer(s) are still expected by this session, '
                                   'but its background waiter is not running — it died with the harness '
                                   'restart. Sort this out before answering the user:')
CLAUDE_HOOK_DSH_REARM_HEAD_READ = ('DSH: the waiter collected subagent answers while this session was '
                                   'interrupted — read the ones below that you have not read yet:')
CLAUDE_HOOK_DSH_REARM_RUN = ('Re-arm the waiter NOW as a background task (run_in_background: true); '
                             'its exit wakes you when the rest arrives:\n'
                             '  cd /tmp && dsh-board wait --out-dir {out}')


def claude_hook_dsh_rearm_context(data: dict) -> str:
    """Текст для `additionalContext` по вводу хука; пусто — сказать нечего, молчим."""
    owner = str((data or {}).get('session_id') or os.environ.get(DSH_WAIT_OWNER_ENV) or '')
    if not owner:
        return ''

    out_dir = _out_dir(owner)
    state = _state_read()
    ready = _collected_answers(state, out_dir)
    pending = dsh_wait_pending(owner)
    alive = dsh_wait_waiter_alive(owner)

    # «Входящие» хоста — только когда они что-то решают: метки есть, а ждать их некому.
    answered = _answered_guarded(pending) if pending and not alive else []
    got = {dsh_wait_key(str(row.get('session') or '')): row for row in answered}
    seen_cards = {row['card'] for row in ready}

    lines = []
    if ready:
        _state_write({row['path']: row['mtime'] for row in ready})
        lines.append(_ready_line(ready, got, out_dir))

    uncollected = [row for key, row in got.items() if key not in seen_cards]
    if uncollected:
        names = ', '.join(f"\"{row['title']}\"" if row.get('title') else dsh_wait_key(str(row['session']))[:8]
                          for row in uncollected)
        lines.append(f'{len(uncollected)} answer(s) sit in the inbox, never collected: {names}')

    if pending and not alive:
        running = [key for key in pending if key not in got and key not in seen_cards]
        if running:
            lines.append(f'{len(running)} subagent(s) still running: ' +
                         ', '.join(key.replace('continuous-', '')[:8] for key in running))
        head = CLAUDE_HOOK_DSH_REARM_HEAD_DEAD.format(count=len(pending))
        lines.append(CLAUDE_HOOK_DSH_REARM_RUN.format(out=out_dir))
    else:
        head = CLAUDE_HOOK_DSH_REARM_HEAD_READ

    return '\n'.join([head, *lines]) if lines else ''


def main() -> int:
    """Печатает `additionalContext` по договору хука; код возврата всегда 0."""
    data = claude_hook_input()
    try:
        context = claude_hook_dsh_rearm_context(data)
    except Exception as bad:                        # noqa: BLE001 — хук не враг старту сессии
        print(f'claude_hook_dsh_rearm: {bad}', file=sys.stderr)
        return 0
    if not context:
        return 0
    print(json.dumps({'hookSpecificOutput': {'hookEventName':
                                            str(data.get('hook_event_name') or 'SessionStart'),
                                            'additionalContext': context}}, ensure_ascii=False))
    return 0


# --- Приватные: ниже всех публичных (code_rules.md, §3) -----------------------

def _out_dir(owner: str) -> str:
    """Каталог ждуна этой сессии — тот же, что подставляет застава `claude_hook_dsh_wait`."""
    return os.path.join(os.environ.get('TMPDIR') or '/tmp', f'dsh-wait-{owner[:8]}')


def _collected_answers(state: dict, out_dir: str) -> list:
    """Свежие ответы, собранные ждуном и ещё не показанные: [{card, path, mtime}], свежие сверху.

    Обходятся каталог `dsh-wait-<id8>` и его суффиксные копии (`…-2`, `…-check`: модель
    иной раз заводит под перестановку отдельный). Старше `DSH_WAIT_MAX_AGE` не
    показываем — те же часы, что отдают метки.
    """
    edge = time.time() - DSH_WAIT_MAX_AGE
    out = []
    for seat in _out_dirs(out_dir):
        for found in seat.glob('*.md'):
            try:
                mtime = found.stat().st_mtime
            except OSError:
                continue
            path = str(found)
            if mtime <= edge or abs(float(state.get(path) or 0) - mtime) < 0.001:
                continue                            # слишком старый для отчёта или уже показан
            out.append({'card': dsh_wait_key(found.stem), 'path': path, 'mtime': mtime})
    out.sort(key=lambda row: -row['mtime'])
    return out


def _ready_line(ready: list, got: dict, out_dir: str) -> str:
    """Строка «готовые ответы»: подпись карточки, если хост её назвал, и путь к файлу."""
    items = []
    for row in ready[:CLAUDE_HOOK_DSH_REARM_SHOWN]:
        title = str((got.get(row['card']) or {}).get('title') or '')
        items.append(f'"{title}" -> {row["path"]}' if title else row['path'])
    line = f'{len(ready)} answer(s) ready: ' + '; '.join(items)
    if len(ready) > CLAUDE_HOOK_DSH_REARM_SHOWN:
        line += f' (+{len(ready) - CLAUDE_HOOK_DSH_REARM_SHOWN} more in {out_dir})'
    return line


def _answered_guarded(pending: list) -> list:
    """`dsh_wait_answered` с потолком: хост под зависшим кэшем не будит сессию дольше секунды.

    ⚠ Нить — не красота, а страховка: у `session/list` на холодном кэше потолок 90 с,
    и в лоб вызов мог бы встать на весь бюджет хука. Не успел — отчёт без «входящих»,
    метки-то локальные.
    """
    box = []

    def _ask():
        try:
            box.extend(dsh_wait_answered(pending))
        except Exception as bad:                    # noqa: BLE001 — хост молчит — не знаем про ответ
            print(f'claude_hook_dsh_rearm: inbox недоступен: {bad}', file=sys.stderr)

    worker = threading.Thread(target=_ask, daemon=True)
    worker.start()
    worker.join(CLAUDE_HOOK_DSH_REARM_HOST_WAIT)
    return box


def _out_dirs(out_dir: str) -> list:
    """Каталог ждуна и его суффиксные копии (`dsh-wait-<id8>-2`): префикс — те же 8 символов id."""
    base = Path(out_dir)
    try:
        return [seat for seat in base.parent.glob(base.name + '*') if seat.is_dir()]
    except OSError:
        return [base] if base.is_dir() else []


def _home() -> str:
    """`~/.dsh` (или `DSH_HOME`) — тот же корень, что у меток `dsh_wait`."""
    return os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT)


def _state_path() -> str:
    return os.path.join(_home(), CLAUDE_HOOK_DSH_REARM_STATE)


def _state_read() -> dict:
    try:
        with open(_state_path(), encoding='utf-8') as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def _state_write(seen: dict) -> None:
    """Запомнить показанные файлы; прощавшееся — подчистить: файл удалён или вышел из окна."""
    edge = time.time() - DSH_WAIT_MAX_AGE
    rows = _state_read()
    rows.update(seen)
    for key in [key for key, mtime in rows.items()
                if not isinstance(mtime, (int, float)) or mtime <= edge]:
        rows.pop(key)
    try:
        os.makedirs(_home(), exist_ok=True)
        path = _state_path()
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(rows, handle)
        os.replace(tmp, path)
    except OSError:                                 # без записи напоминание повторится — не беда
        pass


if __name__ == '__main__':
    sys.exit(main())

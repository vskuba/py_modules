"""
Застава «ответ субагента не уходит в никуда»: ход не закрывается, пока ответов ждать некому.

Хук Claude Code на `Stop`. Оркестратора будят только его фоновые процессы; субагент,
заканчивающий работу без такого процесса, заканчивает её в пустоту — так и было: задача
ушла без `--wait`, ход закрыт, ответ лежал во «входящих», пока человек не спросил.

Застава смотрит метки `dsh_wait` этой сессии (каждый промпт субагенту их ставит) и живого
ждуна `dsh-board wait`. Метки есть, ждуна нет — ход не отпускается, модель получает
готовую команду. Ждун жив или меток нет — ход свободен. Поломка самой заставы ход
отпускает (`claude_hook_stop_run`): застава держит только то, в чём уверена.

    echo '{"session_id":"s"}' | python3 claude_/hook/claude_hook_dsh_wait.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from claude_.hook.claude_hook import claude_hook_stop_run  # noqa: E402
from dsh_.dsh_wait import dsh_wait_pending, dsh_wait_waiter_alive  # noqa: E402

CLAUDE_HOOK_DSH_WAIT_TEXT = (
    'У этой сессии субагенты DSH ещё работают, и их ответа никто не ждёт: {count} шт. ({names}). '
    'Закончишь ход так — их ответ уйдёт в никуда. Запусти ждуна ФОНОМ (run_in_background: true) '
    'и только потом заканчивай ход — он разбудит тебя, когда ответы придут:\n'
    '  cd /tmp && dsh-board wait --out-dir {out}')


def claude_hook_dsh_wait_check(data: dict) -> str:
    """Замечание заставы для ввода хука `Stop`; пусто — ход отпущен."""
    owner = data.get('session_id') or ''
    if not owner:
        return ''
    pending = dsh_wait_pending(owner)
    if not pending or dsh_wait_waiter_alive(owner):
        return ''
    out = os.path.join(os.environ.get('TMPDIR') or '/tmp', f'dsh-wait-{owner[:8]}')
    names = ', '.join(key.replace('continuous-', '')[:8] for key in pending[:5])
    return CLAUDE_HOOK_DSH_WAIT_TEXT.format(count=len(pending), names=names, out=out)


if __name__ == '__main__':
    sys.exit(claude_hook_stop_run(claude_hook_dsh_wait_check))

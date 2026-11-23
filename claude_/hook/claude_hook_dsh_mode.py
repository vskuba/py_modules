"""
Режим DSH (`high`/`free`/`off`) — в контекст каждого хода и переключение по `/dsh <режим>`.

Хук Claude Code на `UserPromptSubmit`. Правило режима (`dsh_/dsh_mode`) приходит модели
с каждым сообщением человека: совет из CLAUDE.md модель вспоминала через раз, а
напоминание на каждом ходу забыть нельзя. Сообщение `/dsh high|free|off` хук ловит
сам: пишет режим сессии до того, как модель его прочтёт, — модели остаётся подтвердить.
Поломка хука ход не держит: молча пусто.

    echo '{"session_id":"s","prompt":"/dsh free"}' | python3 claude_/hook/claude_hook_dsh_mode.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_.dsh_mode import dsh_mode_get, dsh_mode_rule, dsh_mode_set, dsh_mode_switch  # noqa: E402


def claude_hook_dsh_mode_context(data: dict) -> str:
    """Текст для контекста модели: правило режима, а при переключении — и отчёт о нём."""
    session = data.get('session_id') or ''
    switched = dsh_mode_switch(data.get('prompt', ''))
    if switched and session:
        dsh_mode_set(switched, session=session)
        return (f'Человек переключил режим DSH этой сессии на {switched.upper()} — хук уже '
                f'записал его. Подтверди одной строкой, больше ничего не делай.\n'
                + dsh_mode_rule(switched))
    return dsh_mode_rule(dsh_mode_get(session))


def main() -> int:
    try:
        context = claude_hook_dsh_mode_context(json.load(sys.stdin))
    except Exception:                        # хук не держит ход ни при какой поломке
        return 0
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                             'additionalContext': context}}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

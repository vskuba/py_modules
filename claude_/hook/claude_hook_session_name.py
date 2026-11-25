"""
Префикс `t ` в сообщении человека — название сессии, а не задача модели.

Хук Claude Code на `UserPromptSubmit`. `t Ссылки в статусной строке` запоминает название
(`claude_session_name_set`) — его показывает таблица сессий в статусной строке — и
**не пускает сообщение к модели** (`decision: block`): человек видит подтверждение, а
токены не тратятся. Ключи — `session_id` из ввода хука и процесс `claude`, предок хука
(после `/resume` внутри сессии id разговора и файла сессии расходятся).

Заодно любой другой промпт человека — знак, что сессию смотрят: снимается «Ready»
(`claude_session_ready_seen`).

    echo '{"prompt":"t Мой проект","session_id":"x"}' | python3 claude_/hook/claude_hook_session_name.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from claude_.session.claude_session_name import claude_session_name_set  # noqa: E402
from claude_.session.claude_session_ready import claude_session_ready_seen  # noqa: E402
from proc_.proc_ps import proc_ps_ancestors  # noqa: E402

# `t ` в начале, строчная: заглавная «T» — обычное начало фразы. Название — одна строка.
CLAUDE_HOOK_SESSION_NAME_RE = re.compile(r'^\s*t\s+(?P<name>[^\n]+?)\s*$')

# Длиннее — уже не название, а текст.
CLAUDE_HOOK_SESSION_NAME_MAX = 120


def claude_hook_session_name_match(prompt: str) -> str:
    """Название из сообщения `t <название>`; не оно — пусто."""
    match = CLAUDE_HOOK_SESSION_NAME_RE.match(prompt or '')
    if not match or len(match.group('name')) > CLAUDE_HOOK_SESSION_NAME_MAX:
        return ''
    return match.group('name')


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    pid = _claude_hook_session_name_pid()
    session_id = data.get('session_id') or ''
    name = claude_hook_session_name_match(data.get('prompt', ''))
    try:
        claude_session_ready_seen(session_id, pid)
        if name:
            claude_session_name_set(name, session_id, pid)
    except Exception as error:  # хук при своей поломке пропускает, а не держит
        print(f'claude_hook_session_name: {error}', file=sys.stderr)
        return 0
    if name:
        print(json.dumps({'decision': 'block', 'reason': f'Название сессии: «{name}»'}, ensure_ascii=False))
    return 0


def _claude_hook_session_name_pid() -> int:
    # Процесс `claude`, запустивший хук: ближайший предок с таким именем.
    for ancestor in proc_ps_ancestors(os.getpid()):
        try:
            with open(f'/proc/{ancestor}/comm', encoding='utf-8') as handle:
                if handle.read().strip().startswith('claude'):
                    return ancestor
        except OSError:
            continue
    return 0


if __name__ == '__main__':
    sys.exit(main())

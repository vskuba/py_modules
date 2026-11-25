"""Транскрипт сессии Claude Code: где лежит.

Транскрипт — `~/.claude/projects/<папка-слагом>/<sessionId>.jsonl`; слаг — путь проекта,
где всё, кроме латиницы и цифр, заменено на `-`.

Что в нём — токены и заголовок — дочитывает `claude_session_tokens`.
"""
import glob
import os
import re

# Где лежат транскрипты.
CLAUDE_SESSION_TRANSCRIPT_ROOT = '~/.claude/projects'


def claude_session_transcript_path(session_id: str, cwd: str = '') -> str:
    """Путь к транскрипту сессии; `cwd` экономит обход всех проектов. Нет — пусто."""
    root = os.path.expanduser(CLAUDE_SESSION_TRANSCRIPT_ROOT)
    if cwd:
        guess = os.path.join(root, re.sub(r'[^A-Za-z0-9]', '-', cwd), f'{session_id}.jsonl')
        if os.path.exists(guess):
            return guess
    found = glob.glob(os.path.join(root, '*', f'{session_id}.jsonl'))
    return found[0] if found else ''


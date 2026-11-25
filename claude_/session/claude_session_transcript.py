"""Транскрипт сессии Claude Code: где лежит и какой у сессии заголовок.

Транскрипт — `~/.claude/projects/<папка-слагом>/<sessionId>.jsonl`; слаг — путь проекта,
где всё, кроме латиницы и цифр, заменено на `-`.

Заголовок — то, что Claude Code ставит в шапку вкладки терминала («✳ Тема»): последняя
запись `custom-title` (сессию переименовал человек), иначе последняя `ai-title`.
⚠ Запись `custom-title` вживую не встречалась: имя поля (`customTitle` или `title`) —
по аналогии с `ai-title`/`aiTitle`, принимаются оба.
"""
import glob
import json
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


def claude_session_transcript_title(session_id: str, cwd: str = '') -> str:
    """Заголовок сессии, как он стоит в шапке вкладки терминала; нет — пусто.

    ⚠ Читает транскрипт целиком (десятки МБ) — для клика, не для перерисовки раз в секунду.
    """
    path = claude_session_transcript_path(session_id, cwd)
    custom = ai = ''
    try:
        with open(path, 'rb') as handle:
            for line in handle:
                if b'-title"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get('type') == 'custom-title':
                    custom = entry.get('customTitle') or entry.get('title') or custom
                elif entry.get('type') == 'ai-title':
                    ai = entry.get('aiTitle') or ai
    except OSError:
        return ''
    return custom or ai

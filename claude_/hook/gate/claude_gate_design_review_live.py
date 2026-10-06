#!/usr/bin/env python3
"""Застава дизайна на конце хода: правку страницы отпустили — спросить, смотрели ли вживую.

Правило, которое она сторожит, живёт в `py_modules/docs/frontend_check_rules.md`
и повторено в `~/.dsh/AGENTS.md`: **правка без снятого кадра — гипотеза, а не работа**.
Ход, в котором тронули `.html`/`.css`/`.jsx`, но так и не сняли страницу, выглядит
готовым ровно настолько, насколько выглядит непроверенное. Застава возвращает ход
назад с одним вопросом: где кадр.

Вешается на точку `Stop` в `~/.claude/settings.json`:

    "Stop": [{"hooks": [{"type": "command",
        "command": "/usr/bin/python3 /home/vasyl/PycharmProjects/py_modules/claude_/hook/gate/claude_gate_design_review_live.py",
        "timeout": 30}]}]

## Что она видит и чего не видит

Видит: последние записи `.jsonl`-транскрипта — правки инструментов `Write`/`Edit`/
`MultiEdit`/`NotebookEdit` в файлы вёрстки и кадры, снятые `browser_take_screenshot`
или `web_shot`/`web_drive --shot` через `Bash`. Дальше арифметика порядка: правка
**после** последнего кадра — не снята; кадр после последней правки — принята.

Не видит (и честно отходит): правки, втиснутые в `Bash` (`sed -i`, `cat >`), — разбор
команды под каждую оболочку не окупается ценой ложных держаний. Отсюда правило:
**застава держит только то, во что уверена**.

## ⚠⚠ Держит дважды и отпускает

Застава, отменяющая всегда, — это застава, которую снимают в тот же вечер, поэтому
своё слово она имеет **два раза на эпизод**: после второй придирки отпускает ход,
записку в памяти гасит, и эпизод начинается заново с ближайшей новой правки. Считает
сама и по своей метке, а не по `stop_hook_active`: этот флаг значит лишь «ход уже
возвращали», а наш предел — сколько раз мы успели спросить.

⚠ Забыть записать придирку — значит держать вечно: проверка неидемпотентна по
своей природе (между проверками агент правит снова), поэтому состояние пишется
**внутри** проверки, а сбой записи глотается — лучше выпустить без счёта, чем
держать без конца.

## Как отойти

    CLAUDE_GATE_DESIGN_REVIEW_LIVE_PASS=1                # один ход, без правки конфига
"""
import os
import sys
from pathlib import Path

# ⚠ Загрузка через `spec_from_file_location` (так делает сюита) не кладёт корень
# слоя в путь поиска сама, а запуск файла по пути — кладёт через sys.path[0].
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from claude_.hook.claude_hook import (claude_hook_stop_run,                   # noqa: E402
                                 claude_hook_transcript_rows)
from claude_.hook.gate.claude_gate import (claude_gate_asked,                 # noqa: E402
                                      claude_gate_forget, claude_gate_note,
                                      claude_gate_noted, claude_gate_off,
                                      claude_gate_remember)

CLAUDE_GATE_DESIGN_REVIEW_LIVE_SEEN = Path.home() / '.cache' / 'claude_gate_design_review_live_seen.json'
# Вердикт «веб ли проект» живёт отдельно и сутки: обход дерева на каждом конце хода
# стоил бы дороже проверки. Промах в плюс — проект, у которого вёрстка появилась
# сегодня, застава увидит завтра.
CLAUDE_GATE_DESIGN_REVIEW_LIVE_WEB = Path.home() / '.cache' / 'claude_gate_design_review_live_web.json'
CLAUDE_GATE_DESIGN_REVIEW_LIVE_ASKS = 2                              # придирок на эпизод, третью не будет
CLAUDE_GATE_DESIGN_REVIEW_LIVE_ROWS = 400                            # записей транскрипта на разбор
CLAUDE_GATE_DESIGN_REVIEW_LIVE_PASS = 'CLAUDE_GATE_DESIGN_REVIEW_LIVE_PASS'
CLAUDE_GATE_DESIGN_REVIEW_LIVE_RULES = 'py_modules/docs/frontend_check_rules.md'
# То, что правят глазами: расширение или имя каталога решают, что файл — вёрстка.
CLAUDE_GATE_DESIGN_REVIEW_LIVE_SUFFIXES = ('.html', '.htm', '.xhtml', '.css', '.scss', '.sass', '.less',
                        '.styl', '.jsx', '.tsx', '.vue', '.svelte', '.astro', '.svg')
CLAUDE_GATE_DESIGN_REVIEW_LIVE_DIRS = ('/templates/', '/static/', '/assets/', '/public/', '/views/',
                    '/frontend/', '/www/', '/webapp/')
CLAUDE_GATE_DESIGN_REVIEW_LIVE_DIR_NAMES = tuple(part.strip('/') for part in CLAUDE_GATE_DESIGN_REVIEW_LIVE_DIRS)
# Дерево на «вебовой ли проект» смотрим сверху: глубокий обход большого репозитория
# на каждом конце хода стоит дороже самой проверки.
CLAUDE_GATE_DESIGN_REVIEW_LIVE_WALK_DEPTH = 4
CLAUDE_GATE_DESIGN_REVIEW_LIVE_WALK_FILES = 3000                   # файлов на обход; дальше — не наш проект
CLAUDE_GATE_DESIGN_REVIEW_LIVE_WALK_SKIP = ('node_modules', '.git', 'dist', 'build', '.venv', 'venv',
                         '__pycache__', '.next', '.cache', 'vendor')
# Что считается кадром: настоящий снимок страницы, а не дамп DOM и не просмотр
# картинки, снятой до правки.
CLAUDE_GATE_DESIGN_REVIEW_LIVE_SHOT_TOOLS = ('browser_take_screenshot',)
CLAUDE_GATE_DESIGN_REVIEW_LIVE_SHOT_COMMANDS = ('web_shot', 'web_shot_page', '--shot')
CLAUDE_GATE_DESIGN_REVIEW_LIVE_EDIT_TOOLS = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit')


def claude_gate_design_review_live(transcript_path, session_id, seen_path=None, now=None) -> dict:
    """Снять вердикт с транскрипта: отпущен ход или в нём правка без кадра.

    Args:
        transcript_path: путь к `.jsonl` из ввода хука.
        session_id: id сессии — по нему живёт счётчик придирок.
        seen_path: где лежит память про эпизод; пусто — `CLAUDE_GATE_DESIGN_REVIEW_LIVE_SEEN`.
        now: epoch «сейчас»; только для тестов.

    ⚠ Путь по умолчанию берётся **в момент вызова**, а не в момент объявления:
    привязанный аргументом файл нельзя подменить в тесте, и проверка натыкалась бы
    на живую память хозяина.

    Returns:
        dict: {'block': bool, 'found': [правки...], 'why': str}.
            Правки перечислены, когда они есть, — при `block` непустые всегда.
    """
    seen_path = Path(seen_path or CLAUDE_GATE_DESIGN_REVIEW_LIVE_SEEN)
    edits = claude_gate_design_review_live_edits(transcript_path)

    if not edits:
        claude_gate_forget(session_id, seen_path)

        return {'block': False, 'found': [], 'why': 'правок вёрстки после кадра нет'}

    if claude_gate_asked(session_id, seen_path, now=now) >= CLAUDE_GATE_DESIGN_REVIEW_LIVE_ASKS:
        claude_gate_forget(session_id, seen_path)

        return {'block': False, 'found': edits,
                'why': f'придирок {CLAUDE_GATE_DESIGN_REVIEW_LIVE_ASKS} из {CLAUDE_GATE_DESIGN_REVIEW_LIVE_ASKS} — держать нечем'}

    claude_gate_remember(session_id, seen_path, now=now)

    return {'block': True, 'found': edits, 'why': 'правка вёрстки без снятого кадра'}


def claude_gate_design_review_live_edits(transcript_path, rows=None) -> list:
    """Файлы вёрстки, правленные после последнего кадра (пусто — кадров не было).

    Args:
        transcript_path: `.jsonl` сессии.
        rows: готовые записи вместо чтения файла; для тестов и переиспользования.

    Returns:
        list: пути правок в порядке транскрипта, без повторов.
    """
    rows = claude_hook_transcript_rows(transcript_path, CLAUDE_GATE_DESIGN_REVIEW_LIVE_ROWS) if rows is None else rows
    edits = []

    for call in _calls(rows):
        if _is_shot(call):
            del edits[:]                                  # кадр снимает подозрение со всего, что было до него
            continue

        path = _edited_path(call)

        if path and _is_layout(path) and path not in edits:
            edits.append(path)

    return edits


def claude_gate_design_review_live_format(verdict) -> str:
    """Собрать замечание, которое агент прочитает вместо конца хода."""
    found = ', '.join(verdict['found'][:6])
    more = '' if len(verdict['found']) <= 6 else f' … и ещё {len(verdict["found"]) - 6}'

    return (
        f'СТОП: правка без снятого кадра — гипотеза, а не работа ({CLAUDE_GATE_DESIGN_REVIEW_LIVE_RULES}).'
        f' Трогнуто вёрстки: {found}{more}.'
        ' Не отпускаю ход: снять кадр этой страницы и посмотреть в него.'
        ' Кадр — снимок экрана страницы (`browser_take_screenshot`,'
        ' `python -m web_.web_shot страница.html --out /tmp/кадр.png`),'
        ' дамп DOM кадром не считается.'
        ' Дальше по порядку приёма: числа из DOM, арифметика геометрии, кадр глазами,'
        ' before/after сравнены, консоль чистая, тронутый элемент пройдёт открытый'
        ' список аспектов § 10.'
        ' Страницы, которую нельзя запустить, это не касается — скажи прямо, и больше'
        ' я к этому ходу не вернусь.'
    )


def main() -> int:
    if claude_gate_off(CLAUDE_GATE_DESIGN_REVIEW_LIVE_PASS):
        return 0

    return claude_hook_stop_run(_look)


def _look(data) -> str:
    transcript = str(data.get('transcript_path') or '')
    session = str(data.get('session_id') or '')

    if not transcript or not session:
        return ''

    if not _web_project(str(data.get('cwd') or os.getcwd())):
        return ''

    verdict = claude_gate_design_review_live(transcript, session)

    return claude_gate_design_review_live_format(verdict) if verdict['block'] else ''


def _calls(rows):
    """Вызовы инструментов из записей транскрипта, в порядке хода."""
    for row in rows:
        message = row.get('message') or {}

        for part in message.get('content') or []:
            if isinstance(part, dict) and part.get('type') == 'tool_use':
                yield part


def _edited_path(call) -> str:
    if call.get('name') not in CLAUDE_GATE_DESIGN_REVIEW_LIVE_EDIT_TOOLS:
        return ''

    tool_input = call.get('input') or {}

    return str(tool_input.get('file_path') or tool_input.get('notebook_path') or '')


def _is_shot(call) -> bool:
    if str(call.get('name') or '').endswith(CLAUDE_GATE_DESIGN_REVIEW_LIVE_SHOT_TOOLS):
        return True

    command = str((call.get('input') or {}).get('command') or '')

    return any(marker in command for marker in CLAUDE_GATE_DESIGN_REVIEW_LIVE_SHOT_COMMANDS)


def _is_layout(path) -> bool:
    if any(marker in path for marker in CLAUDE_GATE_DESIGN_REVIEW_LIVE_DIRS):
        return True

    return path.lower().endswith(CLAUDE_GATE_DESIGN_REVIEW_LIVE_SUFFIXES)


def _web_project(cwd, seen_path=CLAUDE_GATE_DESIGN_REVIEW_LIVE_WEB) -> bool:
    """Вебовой ли проект: в дереве есть статика вёрстки.

    Вердикт запоминается на сутки: обход дерева на каждом конце хода стоит дороже
    самой проверки, а состав проекта за сутки не меняется.

    Args:
        cwd: рабочий каталог сессии из ввода хука.
        seen_path: где лежит память о вердиктах (в тестах — подменный файл).

    Returns:
        bool: True — проект с вёрсткой; False — не веб, каталога нет или обход
            упёрся в потолок. Все три значат «застава отходит».
    """
    try:
        key = str(Path(cwd).resolve())
    except OSError:
        return False

    if not key or not Path(key).is_dir():
        return False

    known = claude_gate_noted(key, seen_path)

    if 'web' in known:
        return bool(known['web'])

    verdict = _walk_layout(Path(key))

    claude_gate_note(key, seen_path, {'web': bool(verdict)})

    return verdict


def _walk_layout(root) -> bool:
    """Есть ли в дереве вёрстка: глубина и число файлов ограничены, вниз не ходим.

    ⚠ Имя каталога-маркера (`www/`, `static/`) — это **находка**, а не преграда:
    свалить его в одну кучу с `node_modules` значит никогда не признать веб-проект,
    у которого вёрстка живёт в таком каталоге, а не россыпью по корню.
    """
    stack = [(root, 0)]
    files = 0

    while stack:
        spot, depth = stack.pop()

        try:
            entries = list(spot.iterdir())
        except OSError:
            continue

        for entry in entries:
            name = entry.name

            if entry.is_symlink():
                continue

            if entry.is_dir():
                if name in CLAUDE_GATE_DESIGN_REVIEW_LIVE_DIR_NAMES:
                    return True

                if name in CLAUDE_GATE_DESIGN_REVIEW_LIVE_WALK_SKIP or name.startswith('.'):
                    continue

                if depth + 1 < CLAUDE_GATE_DESIGN_REVIEW_LIVE_WALK_DEPTH:
                    stack.append((entry, depth + 1))
            elif _is_layout(name):
                return True

            files += 1

            if files >= CLAUDE_GATE_DESIGN_REVIEW_LIVE_WALK_FILES:
                return False

    return False


if __name__ == '__main__':
    sys.exit(main())

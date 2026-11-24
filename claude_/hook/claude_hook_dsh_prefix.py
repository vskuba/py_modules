"""
Префикс `d ` в сообщении человека — задача субагенту DeepSeek Harness, а не самому агенту.

Хук Claude Code на `UserPromptSubmit`. Сообщение, начинающееся с `d ` (или `d:`), не
исполняется оркестратором самим: хук добавляет в контекст инструкцию отдать задачу
субагенту через `dsh-board` (общий слой `dsh_/dsh_board`) и проверить его ответ.
Рабочая папка субагента — папка текущей сессии Claude Code (`cwd` из ввода хука):
карточка DeepSeek заводится в том же проекте, где работает оркестратор. Решение
остаётся за моделью — хук ничего не запускает и ничего не блокирует.

    echo '{"prompt":"d посмотри новости за 24 часа","cwd":"/proj"}' \\
      | python3 claude_/hook/claude_hook_dsh_prefix.py
"""
import json
import re
import sys

# `d ` или `d:` в начале сообщения; регистр не важен.
CLAUDE_HOOK_DSH_PREFIX_RE = re.compile(r'^\s*d[\s:]+(?P<task>\S.*)$', re.IGNORECASE | re.DOTALL)


def claude_hook_dsh_prefix_context(prompt: str, cwd: str) -> str:
    """Инструкция для модели, если сообщение — задача субагенту; иначе пустая строка."""
    match = CLAUDE_HOOK_DSH_PREFIX_RE.match(prompt or '')
    if not match:
        return ''
    task = match.group('task').strip()
    return (
        'Сообщение начинается с префикса «d» — это задача субагенту DeepSeek Harness '
        '(ферма №4), а не тебе. Не выполняй её сам. Отдай её так:\n'
        f'1. `dsh-board new "{cwd}" "<краткое имя задачи>" --ask "<текст задачи>"` '
        '— карточка в папке текущей сессии Claude Code (это её проект); запусти в фоне '
        '(run_in_background) с `--wait --timeout 1800 --out <файл в scratchpad>`: в вывод придёт '
        'итог в 3–5 строк, полный ответ — в файл (читай его, только если итога мало).\n'
        '   Текст задачи переведи на английский (дешевле в токенах, субагент ответит по-английски), '
        'смысл и детали не теряй.\n'
        '   Картинки из сообщения (скриншоты, `[Image: source: /путь]`) передай субагенту '
        'флагом `--image /путь` на каждую — без них он задачу не поймёт.\n'
        '2. Сразу скажи человеку одной строкой: задача отдана, какая сессия.\n'
        '3. Когда придёт ответ — проверь, что можно проверить (код, факты), и перескажи '
        'кратко по-русски с пометкой, что это ответ субагента. Карточку не архивируй: в ней можно '
        'продолжить разговор через `dsh-board ask`.\n'
        f'Текст задачи без префикса: {task}'
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    context = claude_hook_dsh_prefix_context(data.get('prompt', ''), data.get('cwd') or '.')
    if context:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                                 'additionalContext': context}},
                         ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

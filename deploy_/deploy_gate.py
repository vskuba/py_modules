"""
Застава перед выкладкой: узнать команду выкладки и не пустить её с непройденными проверками.

Работает хуком Claude Code, общим для всех проектов, и той же командой — руками:

    echo '{"tool_name":"Bash","tool_input":{"command":"./deploy.sh"},"cwd":"."}' \\
      | PYTHONPATH=py_modules python -m deploy_.deploy_gate

## ⚠⚠ События `pre_deploy` в Claude Code нет, и это устройство, а не обход

Хуки там вешаются на **свои** события: `PreToolUse`, `PostToolUse`,
`UserPromptSubmit`, `Stop` и прочие. Выкладка для них — обычный запуск `Bash`,
ничем не отличимый от `ls`. Поэтому «перед выкладкой» здесь получается так:
застава стоит на каждом `Bash`, смотрит **текст команды** и просыпается только на
приметах выкладки (`DEPLOY_GATE_MARKS`). На всё остальное отвечает мгновенно и
молча.

⚠ Отсюда цена ошибки в приметах: примета шире нужного — застава лезет в каждую
вторую команду; уже нужного — выкладка уходит непроверенной. Список держится
коротким и проверяемым (`--marks`).

## Как отвечает

| Исход | Код | Что видит агент |
|-------|-----|-----------------|
| не выкладка | 0 | ничего |
| выкладка, проверки чисты | 0 | ничего |
| выкладка, есть `стоп` | 2 | отчёт в stderr — и **команда не запускается** |

Код `2` — договор Claude Code: вызов отменяется, а stderr уходит агенту как
замечание. Не `1`: единица для хука значит «сам хук сломался», и вызов проходит.

## ⚠⚠ Заставу можно обойти, и это намеренно

`DEPLOY_GATE_PASS` в окружении снимает проверку целиком. Выкладка бывает срочной —
починка прода в три часа ночи не должна упираться в порядок функций. Но обход
**виден**: он записан здесь, его надо назвать явно, и он не случается сам.

⚠ Чего застава не делает: не гоняет сюиту, не смотрит прод, не ходит в базу.
Она обязана отвечать за секунды — иначе её снимут вместе с пользой.
"""
import json
import os
import re
import sys

from pathlib import Path

# Файл запускают и путём — из хука он зовётся именно так. Тогда первым в путях лежит
# каталог файла, и `import deploy_` находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy_.deploy_precheck import (deploy_precheck, deploy_precheck_format,
                                     deploy_precheck_stopped)

# Переменная окружения, снимающая заставу. ⚠ Имя длинное намеренно: короткое
# однажды окажется выставленным в общем окружении, и застава замолчит навсегда.
DEPLOY_GATE_PASS = 'DEPLOY_GATE_PASS'

# Код возврата, которым Claude Code отменяет вызов и отдаёт stderr агенту.
DEPLOY_GATE_BLOCK = 2

# Приметы выкладки в тексте команды.
#
# ⚠⚠ Сюда попадает только то, после чего код оказывается **на проде**. `git commit`
# здесь нет: коммит ничего не выкладывает, а застава на каждом коммите — это застава,
# которую снимут к обеду.
#
# ⚠ `git push` — есть, и не только из-за самого прода: у этих проектов `py_modules`
# подмодулем, и порядок пушей значим (сперва общий слой, потом проект). Проверка в
# этой точке — последняя, где правка ещё дешева.
DEPLOY_GATE_MARKS = (
    r'\./deploy\.sh',
    r'\bmake\s+deploy\b',
    r'\bgit\s+push\b',
    r'docker\s+compose[^\n]*\bup\b[^\n]*-d',
    r'\bssh\s+\S*prod\S*[^\n]*docker\s+compose',
)

# Проверки, которые застава не гоняет. ⚠ `pages` требует разбора всех роутеров, и
# на нетронутых шаблонах отвечает шумом; на выкладке её зовут руками.
DEPLOY_GATE_SKIP = ()


def deploy_gate(command, root='.') -> dict:
    """Пропускать ли эту команду.

    Args:
        command: текст команды, которую собираются запустить.
        root: корень проекта, откуда её запускают.

    Returns:
        dict: `{deploy, stopped, found, why}` — выкладка ли это, сколько находок
        держат, сами находки и одна строка о решении. `deploy` ложно — остальные
        поля пусты, и это самый частый ответ.

    ⚠ Проверки запускаются **только** на приметах выкладки: застава стоит на каждом
    `Bash`, и лишние две секунды на `ls` заметит сразу и человек, и агент.
    """
    text = str(command or '')
    mark = _mark_of(text)

    if not mark:
        return {'deploy': False, 'stopped': 0, 'found': [], 'why': ''}

    if os.environ.get(DEPLOY_GATE_PASS):
        return {'deploy': True, 'stopped': 0, 'found': [],
                'why': f'застава снята через {DEPLOY_GATE_PASS}'}

    found = deploy_precheck(root, skip=DEPLOY_GATE_SKIP)
    stopped = deploy_precheck_stopped(found)

    return {'deploy': True, 'stopped': stopped, 'found': found,
            'why': f'примета выкладки: {mark}'}


def deploy_gate_format(verdict) -> str:
    """Замечание для агента: почему выкладка не пошла и что с этим делать."""
    if not verdict.get('stopped'):
        return ''

    return '\n'.join((
        f"Выкладка остановлена заставой: находок «стоп» — {verdict['stopped']}.",
        f"({verdict['why']})",
        '',
        deploy_precheck_format(verdict['found']),
        '',
        'Починить найденное и повторить. Если выкладка срочная и починка ждёт — '
        f'запустить с {DEPLOY_GATE_PASS}=1, назвав это человеку.',
    ))


def deploy_gate_marks(command) -> str:
    """Какая примета выкладки нашлась в команде. Пусто — не выкладка.

    ⚠ Вынесено наружу для проверки самих примет: список короткий, но ошибка в нём
    либо глушит заставу, либо мешает каждой второй команде.
    """
    return _mark_of(str(command or ''))


def main() -> int:
    """Хук `PreToolUse`: код 2 отменяет команду, 0 — пропускает.

    ⚠ Своя ошибка заставы пропускает выкладку, а не останавливает её: сломанный
    хук, запрещающий всё, страшнее непроверенной выкладки — второе видно, первое нет.
    """
    if len(sys.argv) > 1 and sys.argv[1] == '--marks':
        for one in DEPLOY_GATE_MARKS:
            print(one)

        return 0

    data = _payload()
    if data.get('tool_name') not in (None, 'Bash'):
        return 0

    command = (data.get('tool_input') or {}).get('command', '')
    root = data.get('cwd') or '.'

    try:
        verdict = deploy_gate(command, root)
    except Exception as bad:      # noqa: BLE001 — см. ⚠ в докстринге
        print(f'застава сорвалась, выкладка пропущена: {bad}', file=sys.stderr)

        return 0

    if not verdict['stopped']:
        return 0

    print(deploy_gate_format(verdict), file=sys.stderr)

    return DEPLOY_GATE_BLOCK


def _mark_of(text: str) -> str:
    """Первая совпавшая примета выкладки — она же объяснение решения."""
    for one in DEPLOY_GATE_MARKS:
        if re.search(one, text, re.IGNORECASE):
            return one

    return ''


def _payload() -> dict:
    """Ввод хука: JSON на stdin. Не JSON — пустой словарь, и застава молчит.

    ⚠⚠ Сломанный ввод обязан пропускать, а не блокировать: хук стоит на **каждом**
    `Bash`, и упади он — работа встанет целиком, причём необъяснимо для человека.
    """
    try:
        return json.loads(sys.stdin.read() or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


if __name__ == '__main__':
    sys.exit(main())

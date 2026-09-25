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
import os
import re
import subprocess
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
from tool_.tool_hook import tool_hook_run

# Переменная окружения, снимающая заставу. ⚠ Имя длинное намеренно: короткое
# однажды окажется выставленным в общем окружении, и застава замолчит навсегда.
DEPLOY_GATE_PASS = 'DEPLOY_GATE_PASS'

# Инструменты, на которые поставлена застава.
DEPLOY_GATE_TOOLS = ('Bash',)

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
    r'make\s+deploy\b',
    r'git\s+(?:-C\s+\S+\s+)?push\b',
    r'docker\s+compose[^\n]*\bup\b[^\n]*-d',
    r'ssh\s+\S*prod\S*[^\n]*docker\s+compose',
)

# Начало команды: строка целиком, либо после разделителя команд оболочки.
#
# ⚠⚠ Приметы ищутся **только в позиции команды**, а не где угодно в тексте. Без этого
# застава ловила сама себя: строка `echo '{"command":"./deploy.sh"}'` — это `echo`, а не
# выкладка, и первая же проверка заставы оказалась заблокирована собственной приметой.
# Кавычки при этом не разбираются (полного разбора оболочки здесь не будет), но
# «выкладка внутри строкового довода» отсекается именно позицией.
DEPLOY_GATE_HEAD = r'(?:^|\n|;|&&|\|\||\|)\s*(?:[A-Z_]+=\S+\s+)*'

# Проверки, которые застава не гоняет. ⚠ `pages` требует разбора всех роутеров, и
# на нетронутых шаблонах отвечает шумом; на выкладке её зовут руками.
DEPLOY_GATE_SKIP = ()


def deploy_gate(command, root='.') -> dict:
    """Пропускать ли эту команду.

    Args:
        command: текст команды, которую собираются запустить.
        root: каталог, из которого её запускают — у хука это каталог **сессии**.

    Returns:
        dict: `{deploy, stopped, found, why, root}` — выкладка ли это, сколько
        находок держат, сами находки, строка о решении и репозиторий, который
        вправду проверен. `deploy` ложно — остальные поля пусты, и это самый
        частый ответ.

    ⚠ Проверки запускаются **только** на приметах выкладки: застава стоит на каждом
    `Bash`, и лишние две секунды на `ls` заметит сразу и человек, и агент.

    ⚠⚠ Проверяется репозиторий, в котором команда **вправду выполнится**, а не
    каталог сессии. Хук получает `cwd` сессии, и `cd py_modules && git push` из
    проекта проверялся бы по проекту: первый же пуш общего слоя встал из-за долга
    чужого репозитория, к выкладке отношения не имеющего.
    """
    text = str(command or '')
    mark = _mark_of(text)

    if not mark:
        return {'deploy': False, 'stopped': 0, 'found': [], 'why': '', 'root': ''}

    where = _root_of(text, root)

    if os.environ.get(DEPLOY_GATE_PASS):
        return {'deploy': True, 'stopped': 0, 'found': [], 'root': where,
                'why': f'застава снята через {DEPLOY_GATE_PASS}'}

    found = deploy_precheck(where, skip=DEPLOY_GATE_SKIP)
    stopped = deploy_precheck_stopped(found)

    return {'deploy': True, 'stopped': stopped, 'found': found, 'root': where,
            'why': f'примета выкладки: {mark} (проверен {where})'}


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

    ⚠ Разбор ввода, коды возврата и «при своей поломке пропускать» — в `tool_hook`,
    одним местом на обе заставы. Здесь только `--marks` и вопрос про команду.
    """
    if len(sys.argv) > 1 and sys.argv[1] == '--marks':
        for one in DEPLOY_GATE_MARKS:
            print(one)

        return 0

    return tool_hook_run(DEPLOY_GATE_TOOLS, _look)


def _look(data: dict) -> str:
    """Замечание по этой команде либо пусто. Вид проверки для `tool_hook_run`."""
    command = (data.get('tool_input') or {}).get('command', '')

    return deploy_gate_format(deploy_gate(command, data.get('cwd') or '.'))


def _root_of(text: str, cwd) -> str:
    """Репозиторий, в котором команда вправду выполнится.

    Читает из самой команды `cd <путь>` и `git -C <путь>`, затем поднимается до
    корня git-дерева. Ничего не нашлось — остаётся каталог сессии.

    ⚠ Берётся **последний** `cd` в цепочке: `cd /tmp && cd py_modules && git push`
    выполнится в `py_modules`, и проверять надо его.

    ⚠ Корень git ищется `rev-parse`, а не поиском `.git` вверх по дереву: у
    подмодуля `.git` — файл со ссылкой, и наивный поиск отдал бы родителя.
    """
    here = Path(str(cwd or '.'))
    # ⚠ Та же привязка к позиции команды, что у примет: без неё `git -C <путь>`,
    # попавшийся внутри строкового довода, увёл бы проверку в чужой репозиторий —
    # именно это и случилось на первой проверке.
    found = re.findall(DEPLOY_GATE_HEAD + r'cd\s+([^\s&;|]+)', text)
    found += re.findall(DEPLOY_GATE_HEAD + r'git\s+-C\s+([^\s&;|]+)', text)

    for one in found:
        step = Path(one.strip('\'"')).expanduser()
        here = step if step.is_absolute() else here / step

    got = subprocess.run(['git', '-C', str(here), 'rev-parse', '--show-toplevel'],
                         capture_output=True, text=True, check=False)

    return got.stdout.strip() if got.returncode == 0 else str(here)


def _mark_of(text: str) -> str:
    """Первая примета выкладки, стоящая в позиции команды, — она же объяснение."""
    for one in DEPLOY_GATE_MARKS:
        if re.search(DEPLOY_GATE_HEAD + one, text, re.IGNORECASE):
            return one

    return ''


if __name__ == '__main__':
    sys.exit(main())

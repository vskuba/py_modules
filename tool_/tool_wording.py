"""
Где про правку всё ещё сказано словами по-старому: комментарии, докстринги, доки.

`tool_impact` ходит **по имени**: кто зовёт `send_every`, что останется мёртвым
при переименовании. А беда бывает и при живом имени: ключ не переименован, а
**смысл поехал** — было «общий замок на три пула», стало «у каждого пула свой
срок». Имена целы, зовущие целы, а проза везде старая: строка в доке про
«общую норму» переживает правку на месяц, и следующий читатель делает вывод из
неё, а не из кода.

Инструмент отвечает на второй вопрос — **кто про это пишет**: все места, где
имя встречается в прозе (комментарий, докстринг, `.md`), с самой строкой.
Разбор «старое это уже или нет» — дело человека: отсюда `взгляд` по природе,
список мест, а не приговор.

Имена берутся из уезжающего диффа (snake_case-токены изменённых строк — ключи
настроек и столбцы так и пишутся) или называются прямо.
"""
import argparse
import ast
import re
import subprocess
from pathlib import Path

# Имена, которые ищем в прозе: snake_case-токены изменённых строк — ключи
# настроек, столбцы, имена функций. Короче пяти букв предмета нет.
_SNAKE = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')

# Что не обходим никогда: чужой код и мусор дерева.
_SKIP = ('/.venv/', '/.git/', '/__pycache__/', '/node_modules/',
         '/.git_worktree/')


def tool_wording(names=(), root='.', base='origin/master') -> list[dict]:
    """Где эти имена описаны словами — в комментариях, докстрингах, документах.

    Args:
        names: имена; пусто — берём из уезжающего диффа (`base..HEAD` + дерево).
        root: корень проекта.
        base: база диффа для сбора имён.

    Returns:
        [{'name', 'kind', 'where', 'text'}, ...] — `kind`: «комментарий»,
        «докстринг», «док»; `where` — файл:строка; `text` — строка прозы.

    ⚠ Про зовущих тут молчит: кто **зовёт** имя — дело `tool_impact`, этот
    модуль отвечает только за то, где имя **описано** словами.
    """
    base_root = Path(root).resolve()
    wanted = set(names) or tool_wording_changed(base_root, base)
    if not wanted:
        return []

    out = []
    for path in _files(base_root):
        spans = _python_prose(path) if path.suffix == '.py' else None
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue

        for num, line in enumerate(lines, 1):
            prose, kind = _prose(line, spans, num)
            if not prose:
                continue
            out += [{'name': one, 'kind': kind,
                     'where': f'{_short(base_root, path)}:{num}',
                     'text': line.strip()[:140]}
                    for one in wanted if one in prose]
    return out


def tool_wording_changed(root='.', base='origin/master') -> set:
    """Имена из уезжающего диффа: snake_case-токены изменённых строк.

    ⚠ Смотрят и **контекст** вокруг хунки (`-U3`): прозу пугает не только
    новая строка, но и то, что правка тронула рядом.
    """
    out = set()
    for args in (['diff', '-U3', f'{base}...HEAD'], ['diff', '-U3', 'HEAD'],
                 ['diff', '-U3', '--cached']):
        got = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                             text=True, check=False)
        if got.returncode != 0:
            continue
        for line in got.stdout.split('\n'):
            if line.startswith(('+++', '---', '@@')):
                continue
            if line[:1] in ('+', '-', ' '):
                out |= set(_SNAKE.findall(line[1:]))
    return {one for one in out if len(one) >= 5}


def _files(base: Path):
    """Все .py и .md дерева, кроме чужих мест."""
    for ext in ('.py', '.md'):
        for path in base.rglob(f'*{ext}'):
            if not any(one in str(path) for one in _SKIP):
                yield path


def _python_prose(path: Path) -> dict:
    """Номера строк, которые проза python-файла: {строка: «докстринг»}.

    Всё остальное проза — комментарий: строка с `#`. Разбор через `ast`:
    строка внутри докстринга перестает быть строкой только потому, что её
    разобрал анализатор — на глаз её не отличить.
    """
    spans = {}
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    except (SyntaxError, OSError):
        return spans

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, 'body', ())
        if body and ast.get_docstring(node, clean=False) \
                and isinstance(body[0], ast.Expr):
            for num in range(body[0].lineno, body[0].end_lineno + 1):
                spans[num] = 'докстринг'
    return spans


def _prose(line: str, spans, num: int) -> tuple:
    """Проза ли это строка и какая: ('', '') для кода.

    ⚠ Код строки не считается: `send_every = ...` ниже по файлу — это код, им
    ходит `tool_impact`. Сюда проходит только то, что написано про имя.
    """
    if spans is None:
        return line, 'док'                  # .md: всё в файле — проза
    if num in spans:
        return line, 'докстринг'
    if '#' in line:
        return line.split('#', 1)[1], 'комментарий'
    return '', ''


def _short(base: Path, path) -> str:
    try:
        return str(Path(path).resolve().relative_to(base))
    except ValueError:
        return str(path)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Где про имена правки всё ещё сказано словами по-старому.')
    ap.add_argument('names', nargs='*', help='имена; пусто — из уезжающего диффа')
    ap.add_argument('--root', default='.', help='корень проекта')
    ap.add_argument('--base', default='origin/master', help='база диффа')
    args = ap.parse_args()

    found = tool_wording(tuple(args.names), args.root, args.base)
    by = {}
    for one in found:
        by.setdefault(one['name'], []).append(one)
    for name, rows in sorted(by.items()):
        print(f'● {name} — прозы {len(rows)}')
        for one in rows[:8]:
            print(f"    {one['where']} [{one['kind']}] {one['text'][:100]}")
        if len(rows) > 8:
            print(f'    … и ещё {len(rows) - 8}')
    print(f'имён затронуто: {len(by)}, строк прозы: {len(found)}')

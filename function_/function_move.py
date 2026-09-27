"""Перевозка верхнеуровневого определения целиком: шапка, декораторы, тело, швы.

Правило — `docs/code_rules.md` §3: приватное внизу, публичное сверху; правя
файл, порядок правят заодно. До инструмента блок переезжал руками: heredoc с
`assert old in text`, вырез по индексу строки, шов из четырёх пустых строк на
месте выреза. Замерено на одной сессии: четыре шага рефакторинга, два близких
промаха — условие сайта выехало вместе с докстрингом, швы правили вторым
заходом.

## Что делает

* берёт определение целиком: декораторы, тело и шапку-комментарий над ним без
  пустой строки — комментарий переезжает вместе с делом;
* ставит по правилу: между определениями ровно две пустые строки, шапка цели
  не рассекается;
* показывает `needs` — имена, которые переехавший блок читает в источнике и
  которых нет в приёмнике: кандидаты стать параметрами (как `shifts`, едва не
  потерянный при ручном разрезе).

## ⚠ Чего не решает

Не решает, переезжать ли, — перенос меняет порядок чтения и иногда работу,
это решение человека, как у `function_order`. Не сводит близнецов в паттерн —
для этого `function_twin`.
"""
import argparse
import ast
import difflib
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def function_move(path, name: str, *, to=None, before: str = '', after: str = '',
                  apply: bool = False) -> dict:
    """Перенести верхнеуровневое определение — в файле или в другой файл.

    Args:
        path: файл-источник.
        name: имя верхнеуровневого `def`/класса/константы.
        to: приёмник; пусто — тот же файл.
        before: встать перед этим определением приёмника (с его шапкой).
        after: встать после него. Ровно один указатель обязателен.
        apply: записать файлы; без него возвращается только новый текст.

    Returns:
        `{'name', 'origin', 'dest', 'needs', 'added_imports', 'before', 'files'}`:
        `before` и `files` — тексты до и после переезда (записаны только при
        `apply`), `needs` — имена источника, которые блок читает, а в приёмнике
        их нет: их передают параметрами или тоже везут следом.

    Raises:
        RuntimeError: файла нет, имя не найдено, указателя нет или их два,
        указатель в приёмнике не найден.
    """
    src, dst = Path(path), Path(to) if to else Path(path)
    if not src.exists():
        raise RuntimeError(f'нет файла: {path}')
    if bool(before) == bool(after):
        raise RuntimeError('нужен ровно один указатель: before или after')

    cross = str(dst) != str(src)
    if cross and not dst.exists():
        raise RuntimeError(f'нет приёмника: {to}')

    before_texts = {str(src): src.read_text(encoding='utf-8')}
    if cross:
        before_texts[str(dst)] = dst.read_text(encoding='utf-8')
    texts = dict(before_texts)

    origin_tree = ast.parse(before_texts[str(src)])
    node = _find(origin_tree.body, name)
    block = _block_of(texts[str(src)], node)
    texts[str(src)] = _cut(texts[str(src)], node)

    dest_key = str(dst)
    anchor = _find(ast.parse(texts[dest_key]).body, before or after)
    texts[dest_key] = _place(texts[dest_key], anchor, block, bool(before))
    added = []

    if cross:
        line = f'from {_dotted(src)} import {name}'
        texts[dest_key] = _import_into(texts[dest_key], line)
        added = [line]

    helper = _defined(origin_tree)
    dest_defined = _defined(ast.parse(texts[dest_key]))
    needs = sorted(_reads(block) & helper - dest_defined - {name})

    if apply:
        for one, body in texts.items():
            Path(one).write_text(body, encoding='utf-8')

    return {'name': name, 'origin': str(src), 'dest': str(dst), 'needs': needs,
            'added_imports': added, 'before': before_texts, 'files': texts}


def function_move_diff(res: dict) -> str:
    """Диффы переезда: что изменилось в затронутых файлах."""
    out = []

    for one, body in res['files'].items():
        out.extend(difflib.unified_diff(res['before'][one].splitlines(),
                                        body.splitlines(),
                                        fromfile=one, tofile=one + ' →', n=1))
    return '\n'.join(out)


def _block_of(text: str, node) -> str:
    """Блок целиком: шапка-комментарий без пустой строки, декораторы, тело."""
    lines = text.splitlines(keepends=True)
    start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])]) - 1
    while start > 0 and lines[start - 1].lstrip().startswith('#'):
        start -= 1
    return ''.join(lines[start:node.end_lineno]).rstrip('\n')


def _cut(text: str, node) -> str:
    """Текст без блока: на шве остаются ровно две пустые строки."""
    lines = text.splitlines(keepends=True)
    start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])]) - 1
    while start > 0 and lines[start - 1].lstrip().startswith('#'):
        start -= 1
    left = ''.join(lines[:start]).rstrip('\n')
    right = ''.join(lines[node.end_lineno:]).lstrip('\n')
    if not left:
        return right + '\n' if right else ''
    return left + '\n\n\n' + (right + '\n' if right else '')


def _place(text: str, anchor, block: str, edge_before: bool) -> str:
    """Вставить блок к определению: перед его шапкой или после тела."""
    lines = text.splitlines(keepends=True)
    at = anchor.lineno - 1
    if edge_before:
        while at > 0 and lines[at - 1].lstrip().startswith('#'):
            at -= 1
    else:
        at = anchor.end_lineno
    left = ''.join(lines[:at]).rstrip('\n')
    right = ''.join(lines[at:]).lstrip('\n')
    if not left:
        return block + '\n\n\n' + right if right else block + '\n'
    tail = '\n\n\n' + right if right else '\n'
    return left + '\n\n\n' + block + tail


def _import_into(text: str, line: str) -> str:
    """Строку импорта — под последний верхнеуровневый импорт файла."""
    tree = ast.parse(text)
    last = max((n.end_lineno for n in tree.body
                if isinstance(n, (ast.Import, ast.ImportFrom))), default=0)
    lines = text.splitlines(keepends=True)
    if not last:
        return line + '\n\n' + text
    return ''.join(lines[:last]).rstrip('\n') + '\n' + line + '\n' + ''.join(lines[last:]).lstrip('\n')


def _find(body: list, name: str):
    """Верхнеуровневое определение по имени — `def`, класс или константа."""
    for node in body:
        if getattr(node, 'name', '') == name:
            return node
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tg = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in tg):
                return node
    raise RuntimeError(f'нет такого определения: {name}')


def _defined(tree: ast.Module) -> set:
    """Имена, объявленные верхним уровнем модуля."""
    out = set()

    for n in tree.body:
        if getattr(n, 'name', ''):
            out.add(n.name)
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            out |= {t.id for t in tg if isinstance(t, ast.Name)}
    return out


def _reads(block: str) -> set:
    """Имена, которые блок читает."""
    return {n.id for n in ast.walk(ast.parse(block)) if isinstance(n, ast.Name)}


def _dotted(src: Path) -> str:
    """`src/dating/x.py` → `src.dating.x` — как модуль зовут извне."""
    parts = [p for p in Path(src).with_suffix('').parts if p not in ('/', '\\')]
    return '.'.join(parts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Перевезти верхнеуровневое определение целиком (code_rules §3).')
    parser.add_argument('path', help='файл-источник')
    parser.add_argument('name', help='что переезжает')
    parser.add_argument('--to', default=None, help='приёмник; пусто — тот же файл')
    pos = parser.add_mutually_exclusive_group(required=True)
    pos.add_argument('--before', default='', help='встать перед этим определением')
    pos.add_argument('--after', default='', help='встать после него')
    parser.add_argument('--apply', action='store_true', help='записать; без него — дифф')
    args = parser.parse_args()

    try:
        got = function_move(args.path, args.name, to=args.to,
                            before=args.before, after=args.after, apply=args.apply)
        print(function_move_diff(got))
        if got['needs']:
            print('нужно блоку, а в приёмнике нет:', ' '.join(got['needs']))
        if got['added_imports']:
            print('добавлено:', ' '.join(got['added_imports']))
    except (RuntimeError, SyntaxError) as err:
        raise SystemExit(f'ошибка: {err}')

    raise SystemExit(0)

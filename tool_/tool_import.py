"""
Импорты, которые никто в файле не читает: мёртвые провода списком.

    PYTHONPATH=py_modules python -m tool_.tool_import src
    PYTHONPATH=py_modules python -m tool_.tool_import . ; echo $?   # сторож

Мёртвый импорт — не просто мусор: он обещает зависимость, которой файл не
имеет. Читатель (и следующий агент) решает по первой строке, чем живёт модуль,
— а `import random` в файле, где доли уехали в жребий, врал ровно до этого
отчёта. Линтеров в рабочих образах нет и не предвидится, а ручная AST-простыня
на каждый случай не пишется — отсюда модуль.

`tool_dead` судит **имена-определения** по всему репозиторию: у импорта другая
природа — он не определение, а обещание, и проверяется **внутри своего
файла**: прочитано ли имя хоть раз. Две дороги не сливают: у вердиктов разные
основания и разная цена ложного срабатывания.

Что считается прочитанным:
* любое вхождение `ast.Name` (`json.dumps` — прочитали `json`);
* имя в `__all__` — реэкспорт осознан;
* `try: import x / except ImportError: x = None` судится по имени целиком:
  имя читается — живы обе ветки, не читается — мертвы обе, как и есть.

Чего модуль сознательно не знает: имён, собранных строкой (`getattr`,
`importlib.import_module`), и магии каркасов. На такие места — штатная
пометка `# noqa` на строке импорта: инструмент её уважает.
"""
import argparse
import ast
import sys
import warnings
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_.file_walk import FILE_WALK_SKIP, file_walk

# Ветки в обход: к штатным служебным — черновики и чужой submodule общего
# слоя (его судят в нём самом, а не из каждого проекта).
# ⚠ Имена веток, а не пути: `file_walk` режет по имени каталога на любом
# уровне; переданный `skip` заменяет собой молчавший по умолчанию список.
TOOL_IMPORT_SKIP = FILE_WALK_SKIP + ('scratchpad', 'py_modules')

# `__init__.py` не судится вовсе: его импорт по идиому и есть реэкспорт,
# читать его будет другой файл пакета.
TOOL_IMPORT_PACKAGE_FILE = '__init__.py'


def tool_import(root='.') -> list:
    """Импорты, чьё имя не прочитано в том же файле.

    Args:
        root: файл или корень дерева.

    Returns:
        list[dict]: записи `{file, line, name, source}` — где лежит импорт,
        какое имя он обещает и откуда имя берётся. Порядок — по файлу и
        строке. Пусто — мёртвых импортов нет.

    ⚠ Судится **файл целиком**: имя в строке или в `getattr(mod, 'x')` не
    видно. Поэтому находка — кандидат: удаление решает человек (или сторож
    сюиты), а не этот список.
    """
    found = []
    for path in file_walk(root, ('.py',), skip=TOOL_IMPORT_SKIP):
        if path.name == TOOL_IMPORT_PACKAGE_FILE:
            continue
        found += _file_imports(path)
    return sorted(found, key=lambda one: (str(one['file']), one['line']))


def tool_import_format(found: list) -> str:
    """Отчёт словами. Пусто — мёртвых импортов не нашлось."""
    if not found:
        return 'Мёртвых импортов не нашлось.'

    out = [f'Кандидатов: {len(found)}.']
    file_now = ''
    for one in found:
        if str(one['file']) != file_now:
            file_now = str(one['file'])
            out.append(f'\n── {file_now}')
        out.append(f"   {one['line']:>5}  {one['name']:24} из {one['source']}")
    out.append('\n⚠ Имя может читаться строкой (`getattr`, реестр, импорт по '
               'имени) — для таких мест пометка `# noqa` на строке импорта.')
    return '\n'.join(out)


def _file_imports(path: Path) -> list:
    """Импорты файла без тех, чьё имя читается в том же файле.

    ⚠ Файл с синтаксической ошибкой пропускается молча — тот же порядок,
    что у `tool_dead`: инструмент вспомогательный и падать из-за чужой
    недописанной правки не должен.
    """
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
        # ⚠ Ломовые молчат: разбор чужих файлов с устаревшими escape-
        # последовательностями иначе осыпает предупреждениями весь отчёт.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    lines = text.splitlines()

    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= _dunder_all(tree)

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split('.')[0]
                out.append((name, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module == '__future__':
                continue
            origin = '.' * node.level + (node.module or '') or '.'
            for alias in node.names:
                if alias.name == '*':
                    continue
                join = '.' if node.module else ''
                out.append((alias.asname or alias.name, node.lineno,
                            f'{origin}{join}{alias.name}'))

    keep = []
    for name, line, source in out:
        if name in used:
            continue
        if line - 1 < len(lines) and 'noqa' in lines[line - 1]:
            continue
        keep.append({'file': str(path), 'line': line, 'name': name,
                     'source': source})
    return keep


def _dunder_all(tree) -> set:
    """Имена из `__all__` — реэкспорт как чтение."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == '__all__'
                   for t in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
            out |= {one.value for one in node.value.elts
                    if isinstance(one, ast.Constant)
                    and isinstance(one.value, str)}
    return out


def main() -> int:
    """CLI: код возврата — число кандидатов."""
    ap = argparse.ArgumentParser(
        description='Импорты, которые никто в файле не читает.')
    ap.add_argument('root', nargs='?', default='.', help='файл или корень')
    ns = ap.parse_args()
    found = tool_import(ns.root)
    print(tool_import_format(found))
    return len(found)


if __name__ == '__main__':
    raise SystemExit(main())

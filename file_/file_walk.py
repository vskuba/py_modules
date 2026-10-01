"""Файлы дерева без служебных веток: обход, который не заходит в `.venv`.

Обойти репозиторий за исходниками нужно четырём инструментам сразу — описи
(`tool_catalog`), вычитке (`tool_typo`), поиску имени по слоям (`tool_impact`),
сверке разметки (`page_wire`), — и все четверо писали это одинаково:
`rglob('*')` со списком служебных папок в фильтре ПОСЛЕ обхода. Фильтр после
обхода не экономит ничего: дерево виртуалки перебирается целиком и только
потом выбрасывается.

Цифры этого репозитория: `rglob('*.py')` отдаёт 22 237 путей ради 204 своих —
0.31 c; тот же обход с обрезкой веток на входе в каталог — 0.001 c. Триста
раз. На `rglob('*')` (так ищет `tool_impact`) разница ещё грубее: 78 722 пути
против 280.

Отсюда одна функция вместо четырёх копий: список служебных веток и порядок
обхода названы один раз, а вызывающий говорит только, какие расширения ему
нужны.
"""
import argparse
import os

from pathlib import Path

# Ветки, в которые не заходят никогда: чужой код, кеши, служебное. Обрезаются
# ПРИ обходе (`dirs[:]`), а не фильтром после него, — в этом вся экономия.
FILE_WALK_SKIP = ('.venv', 'venv', '__pycache__', '.git', 'node_modules',
                  '.idea', '.mypy_cache', '.pytest_cache', '.ruff_cache',
                  'dist', 'build', '.next')


def file_walk(root='.', suffixes=(), skip=FILE_WALK_SKIP) -> list:
    """Файлы дерева, минуя `.venv`, `.git`, `node_modules` и прочее служебное.

    Args:
        root: корень обхода; файл на входе возвращается сам собой (одним
            элементом) — вызывающему не нужна ветка «а если это файл».
        suffixes: расширения с точкой (`('.py',)`, `('.py', '.md')`);
            пусто — все файлы.
        skip: имена веток, в которые не заходить; по умолчанию
            `FILE_WALK_SKIP`.

    Returns:
        list[Path]: отсортированный список путей от `root` — порядок
        устойчив между запусками, на нём можно сравнивать выдачи.

    ⚠ Ветка обрезается **по имени каталога на любом уровне**, а не по пути:
    `node_modules` не обойдётся ни в корне, ни внутри пакета. Обратная
    сторона — свой каталог с таким именем тоже не обойдётся; назовите его
    иначе, а не расширяйте `skip` исключением.

    ⚠ Симлинки на каталоги не разворачиваются (`os.walk` без `followlinks`):
    `docs-common -> ../py_modules/docs` иначе обошёлся бы дважды.
    """
    base = Path(root)
    if base.is_file():
        return [base] if not suffixes or base.suffix in suffixes else []

    skip = set(skip)
    found = []
    for folder, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if not suffixes or os.path.splitext(name)[1] in suffixes:
                found.append(Path(folder) / name)
    return sorted(found)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Файлы дерева без служебных веток (.venv, .git, '
                    'node_modules): что вообще обойдёт инструмент.')
    ap.add_argument('root', nargs='?', default='.', help='корень обхода')
    ap.add_argument('--suffix', action='append', default=[], metavar='.py',
                    help='расширение с точкой, повторять; пусто — все файлы')
    ap.add_argument('--count', action='store_true', help='только число')
    ns = ap.parse_args()
    paths = file_walk(ns.root, tuple(ns.suffix))
    print(len(paths) if ns.count else '\n'.join(str(p) for p in paths))

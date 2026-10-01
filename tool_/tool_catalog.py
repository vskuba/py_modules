"""
Опись общего слоя: что здесь вообще есть — публичные функции и темы документации.

Реестра и точки входа у py_modules нет, соседний namespace не виден, пока в него не
заглянешь. Опись собирается **на лету** обходом дерева: индекса на диске не существует,
поэтому устаревать нечему — переименованная вчера функция видна сегодня же.

Две половины описи отвечают на разные вопросы. Функция говорит «чем это делают»
(`uvicorn_panel_request`), тема из `docs/readme.md` — «как это устроено и обо что уже
спотыкались». Ищут обычно второе, а зовут первое, поэтому в описи лежат обе.
"""
import ast
import re
import sys

from pathlib import Path

# Файл запускают и путём (`python3 py_modules/tool_/tool_catalog.py`). Тогда первым в путях
# лежит каталог файла, и соседний namespace (`file_`) не находится вовсе.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_.file_walk import file_walk

TOOL_CATALOG_ROOT = Path(__file__).resolve().parents[1]
TOOL_CATALOG_TOPICS = 'docs/readme.md'

# Строка таблицы «Состав» в индексе документации: | `имя.md` | описание |
TOOL_CATALOG_TOPIC_ROW = re.compile(r'^\|\s*`([\w./-]+\.md)`\s*\|\s*(.+?)\s*\|\s*$')


def tool_catalog(root=None) -> list[dict]:
    """Опись библиотеки одним списком — публичные функции и темы документации.

    Args:
        root: корень py_modules; пусто — тот, в котором лежит этот файл.

    Returns:
        list[dict]: записи с полями `kind` (`func` или `topic`), `where` (путь от
        корня), `name`, `summary` (первая строка докстринга либо строка индекса),
        `text` (докстринг целиком — для поиска по мелочи), `cli` (у модуля есть
        `__main__`), `line`.

    ⚠ Обход разбирает синтаксическое дерево, а не импортирует модули: тяжёлые
    зависимости не поднимаются и код файлов не исполняется. Цена — видны только
    функции **уровня модуля**; методы классов в опись не попадают.

    ⚠ Файл с синтаксической ошибкой молча пропускается: опись — вспомогательная
    вещь и падать из-за чужой недописанной правки не должна.
    """
    base = Path(root) if root else TOOL_CATALOG_ROOT
    return _functions(base) + _topics(base)


def _functions(base: Path) -> list[dict]:
    """Публичные функции уровня модуля со всех `*.py` под корнем."""
    out = []

    for path in file_walk(base, ('.py',)):
        try:
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        where = path.relative_to(base).as_posix()
        cli = '__main__' in source

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith('_'):
                continue

            doc = (ast.get_docstring(node) or '').strip()
            out.append({
                'kind': 'func',
                'where': where,
                'name': node.name,
                'summary': _summary(doc),
                'text': doc,
                'cli': cli,
                'line': node.lineno,
            })

    return out


def _topics(base: Path) -> list[dict]:
    """Темы документации — строки таблицы «Состав» из индекса `docs/readme.md`."""
    index = base / TOOL_CATALOG_TOPICS
    try:
        lines = index.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []

    out = []
    for number, line in enumerate(lines, 1):
        found = TOOL_CATALOG_TOPIC_ROW.match(line)
        if not found:
            continue

        name, about = found.group(1), found.group(2)
        # Заголовок таблицы («| Файл | Содержимое |») под шаблон не подходит,
        # а разделительная строка — подходит только в виде пустого имени.
        if not name.endswith('.md'):
            continue

        out.append({
            'kind': 'topic',
            'where': f'docs/{name}',
            'name': name[:-3],
            'summary': about,
            'text': about,
            'cli': False,
            'line': number,
        })

    return out


def _main() -> None:
    """CLI: `python -m tool_.tool_catalog [--kind func|topic] [--json]`."""
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description='Опись общего слоя: публичные функции и темы документации. '
                    'Собирается обходом дерева, индекса на диске нет.')
    ap.add_argument('--kind', default='', choices=['', 'func', 'topic'],
                    help='только функции или только темы')
    ap.add_argument('--namespace', default='',
                    help='только этот namespace (`adb_`, `image_`)')
    ap.add_argument('--cli', action='store_true', help='только то, у чего есть CLI')
    ap.add_argument('--json', action='store_true', help='машинный вывод')
    ns = ap.parse_args()

    rows = [r for r in tool_catalog()
            if (not ns.kind or r['kind'] == ns.kind)
            and (not ns.namespace or r['where'].split('/')[0] == ns.namespace)
            and (not ns.cli or r['cli'])]
    if ns.json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        mark = '🔧' if r['kind'] == 'func' else '📄'
        print(f"{mark} {r['where']}:{r['name']}{'  [CLI]' if r['cli'] else ''}")
        if r['summary']:
            print(f"      {r['summary']}")
    print(f'\nвсего: {len(rows)}')


def _summary(doc: str) -> str:
    """Первая строка докстринга — та, что отвечает «зачем звать» (code_rules, §7.1)."""
    if not doc:
        return ''

    first = doc.split('\n', 1)[0].strip()
    # Докстринг, начатый с новой строки после кавычек: первая строка пустая.
    if not first:
        rest = doc.strip().split('\n', 1)
        first = rest[0].strip() if rest else ''

    return first


if __name__ == '__main__':
    _main()

"""
Опечатки в тексте репозитория, которые видно без словаря: склейки, подмены букв, повторы.

Русского словаря в системе нет, и **без словаря орфографию не проверить** — попытка
обойтись частотностью корпуса провалилась наглядно: у флективного языка законные формы
похожи друг на друга сильнее, чем опечатка на верное слово («сериалу»/«сериал» — 0.92,
«пояснем»/«пояснение» — 0.75). Эвристика по близости ранжирует ровно наоборот, поэтому
её здесь нет.

Осталось то, что ловится **надёжно и без словаря**: следы неудачной правки. Две буквы из
разных алфавитов в одном слове (`косинуc` с латинской `c` — глазами не видно), слипшиеся
при вставке слова (`вcwd`, `иTokenizer`), слово, повторённое дважды. Это не орфография, а
механика, и она не гадает.

Настоящая вычитка — дело человека или языковой модели: `--prose` отдаёт всю прозу
репозитория одним потоком, готовым к отправке (например, `| gx10 "найди опечатки"`).

Отдельный режим `--cyrillic` — уже не проза, а код: дерево `*.py`, и в нём имена
функций и ключи словарей на кириллице, чего правило (`code_rules.md` §1.2) не терпит.
"""
import ast
import re
from pathlib import Path

TOOL_TYPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_TYPO_SKIP = ('.venv', '__pycache__', '.git', 'node_modules', '.idea')

TOOL_TYPO_CYRILLIC = re.compile(r'[а-яёА-ЯЁ]')
TOOL_TYPO_LATIN = re.compile(r'[a-zA-Z]')
TOOL_TYPO_LETTERS = re.compile(r'[^a-zA-Zа-яёА-ЯЁ]+')
TOOL_TYPO_DOUBLED = re.compile(r'\b([а-яё]{3,})\s+\1\b', re.IGNORECASE)
TOOL_TYPO_FENCE = re.compile(r'```.*?```', re.DOTALL)
TOOL_TYPO_INLINE = re.compile(r'`[^`\n]*`')
TOOL_TYPO_URL = re.compile(r'https?://\S+')

# Английское слово с русским окончанием — приём этого репозитория, а не ошибка:
# «JOINы», «switchать». Ошибка выглядит иначе — слипшимися словами или подменой буквы.
TOOL_TYPO_JARGON = re.compile(
    r'^[A-Za-z]{3,}(ы|ов|ам|ами|ах|а|у|е|ать|ают|ает|ал|ила|ится|ов)$')


def tool_typo(root=None) -> list[dict]:
    """Проверить текст на опечатки: склейки, подмены букв, повторы слов.

    Смотрит докстринги и комментарии `*.py` плюс `*.md`; код в обратных кавычках,
    блоки-заборы и адреса вырезаются — там своя орфография.

    Args:
        root: корень репозитория; пусто — тот, где лежит этот файл.

    Returns:
        list[dict]: находки с полями `kind` (`mixed` или `doubled`), `word`,
        `hint`, `where`, `line`.

    ⚠ Орфографию **не проверяет** и проверить не может: словаря нет. Чистый вывод
    означает «механических следов правки нет», а не «текст без ошибок» — за
    смыслом и согласованием идут к `--prose` и живому читателю.

    ⚠ Английское слово с русским окончанием (`JOINы`, `switchать`) — приём этого
    репозитория, из выдачи оно отфильтровано.
    """
    base = Path(root) if root else TOOL_TYPO_ROOT
    prose = _prose(base)
    return _mixed(prose) + _doubled(prose)


def tool_typo_prose(root=None) -> str:
    """Вся проза репозитория одним потоком — на вычитку опечаток человеком или моделью.

    Каждая строка помечена адресом `файл:строка`, чтобы найденное было чем
    исправлять.

    Args:
        root: корень репозитория; пусто — тот, где лежит этот файл.

    Returns:
        str: строки вида `путь:номер\tтекст`.

    ⚠ Объём — сотни килобайт: в промпт целиком не влезет, режьте на части
    (`split`, `head`) либо сужайте корнем.
    """
    base = Path(root) if root else TOOL_TYPO_ROOT
    lines = []
    for where, line, text in _prose(base):
        for piece in text.splitlines():
            if piece.strip():
                lines.append(f'{where}:{line}\t{piece.strip()}')
    return '\n'.join(lines)


def tool_typo_cyrillic(root=None) -> list[dict]:
    """Имена и ключи на кириллице — код, а не проза; правило `code_rules.md` §1.2.

    Смотрит `*.py` синтаксическим деревом: имена функций, ключи литералов-
    словарей и строковые ключи в скобках (`словарь['ключ']`). Что не дерево
    (js, html, sql) деревом не видно — там остаются grep-шаблоны §1.2.

    Args:
        root: корень репозитория; пусто — тот, где лежит этот файл.

    Returns:
        list[dict]: находки с полями `kind` (`cyrillic`), `word`, `where`,
        `line`, `hint`.

    ⚠ Ловит и смешанные имена (`test_metadata_строкой`), а не только целиком
    кирилличные: их набираешь руками с той же раскладки, что и всё вокруг.
    """
    base = Path(root) if root else TOOL_TYPO_ROOT
    out = []
    for path in sorted(base.rglob('*.py')):
        if any(part in TOOL_TYPO_SKIP for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        where = path.relative_to(base).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [(node.name, 'имя функции — по правилу только латиницей')]
            elif isinstance(node, ast.Dict):
                names = [(k.value, 'ключ словаря на кириллице')
                         for k in node.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            elif (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                names = [(node.slice.value, 'ключ словаря на кириллице')]
            else:
                continue
            for name, tip in names:
                if TOOL_TYPO_CYRILLIC.search(name):
                    out.append({'kind': 'cyrillic', 'word': name[:60], 'where': where,
                                'line': getattr(node, 'lineno', 1), 'hint': tip})
    return sorted(out, key=lambda r: (r['where'], r['line']))


def main() -> None:
    """CLI: `python -m tool_.tool_typo [--kind mixed|doubled] [--prose] [--cyrillic]`."""
    import argparse

    ap = argparse.ArgumentParser(
        description='Следы неудачной правки в прозе репозитория: слипшиеся слова, '
                    'подмена буквы из другого алфавита, повтор слова. Плюс — '
                    'кириллица в именах функций и ключах (--cyrillic).')
    ap.add_argument('--kind', default='', choices=['', 'mixed', 'doubled', 'cyrillic'])
    ap.add_argument('--prose', action='store_true',
                    help='не искать, а выдать всю прозу для вычитки моделью')
    ap.add_argument('--cyrillic', action='store_true',
                    help='имена функций и ключи словарей на кириллице в *.py')
    ns = ap.parse_args()

    if ns.prose:
        print(tool_typo_prose())
        return

    if ns.cyrillic or ns.kind == 'cyrillic':
        rows = tool_typo_cyrillic()
        print(f'🔴 ИМЕНА И КЛЮЧИ НА КИРИЛЛИЦЕ — {len(rows)}' if rows
              else 'кириллицы в именах и ключах нет')
        for row in rows:
            print(f"  {row['where']}:{row['line']}  «{row['word']}»  {row['hint']}")
        return

    found = [f for f in tool_typo() if not ns.kind or f['kind'] == ns.kind]
    if not found:
        print('механических следов правки нет '
              '(орфографию это не проверяет — см. --prose)')
        return

    titles = {'mixed': '🔤 БУКВЫ ИЗ РАЗНЫХ АЛФАВИТОВ ИЛИ СЛИПШИЕСЯ СЛОВА',
              'doubled': '👯 СЛОВО ПОВТОРЕНО ДВАЖДЫ'}

    for kind, title in titles.items():
        rows = [f for f in found if f['kind'] == kind]
        if not rows:
            continue
        print(f'\n{title} — {len(rows)}')
        for row in rows:
            print(f"  {row['where']}:{row['line']}  «{row['word']}»  {row['hint']}")


def _prose(base: Path) -> list[tuple]:
    """Проза репозитория: (файл, строка, текст) из докстрингов, комментариев и `*.md`."""
    out = []

    for path in sorted(base.rglob('*.py')):
        if any(part in TOOL_TYPO_SKIP for part in path.parts):
            continue
        try:
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        where = path.relative_to(base).as_posix()

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            doc = ast.get_docstring(node)
            if doc:
                out.append((where, getattr(node, 'lineno', 1), _clean(doc)))

        for number, line in enumerate(source.splitlines(), 1):
            mark = line.find('#')
            # Решётка внутри строкового литерала — не комментарий; грубая, но
            # достаточная проверка: чётное число кавычек слева означает, что
            # литерал к этому месту уже закрылся.
            if mark >= 0 and line[:mark].count('"') % 2 == 0:
                out.append((where, number, _clean(line[mark + 1:])))

    for path in sorted(base.rglob('*.md')):
        if any(part in TOOL_TYPO_SKIP for part in path.parts):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        where = path.relative_to(base).as_posix()
        for number, line in enumerate(_clean(text).splitlines(), 1):
            if line.strip():
                out.append((where, number, line))

    return out


def _clean(text: str) -> str:
    """Убрать из текста код: заборы, обратные кавычки и адреса — там своя орфография."""
    text = TOOL_TYPO_FENCE.sub(' ', text)
    text = TOOL_TYPO_INLINE.sub(' ', text)
    return TOOL_TYPO_URL.sub(' ', text)


def _mixed(prose: list[tuple]) -> list[dict]:
    """Слова, где кириллица и латиница стоят рядом: подмена `с`/`c` или склейка."""
    out = []
    for where, line, text in prose:
        for run in TOOL_TYPO_LETTERS.split(text):
            if len(run) < 2 or TOOL_TYPO_JARGON.match(run):
                continue
            if TOOL_TYPO_CYRILLIC.search(run) and TOOL_TYPO_LATIN.search(run):
                out.append({'kind': 'mixed', 'word': run, 'where': where, 'line': line,
                            'hint': 'подмена буквы или слипшиеся слова'})
    return out


def _doubled(prose: list[tuple]) -> list[dict]:
    """Слово, повторённое подряд дважды, — след правки, а не оборот речи."""
    out = []
    for where, line, text in prose:
        for found in TOOL_TYPO_DOUBLED.finditer(text):
            out.append({'kind': 'doubled', 'word': found.group(0), 'where': where,
                        'line': line, 'hint': 'повтор подряд'})
    return out


if __name__ == '__main__':
    main()

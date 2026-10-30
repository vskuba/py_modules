"""Алфавит проекта: кириллица только в комментариях, иероглифов нет нигде.

Правило языка в проекте знает каждый, а проверить его одним `re` нельзя: в этих
репозиториях комментарии русские, строки — английские, HTML обязан быть чист
целиком, а в комментарии однажды приехал иероглиф — на кириллическом фоне
квадратик глазами выглядит «ещё одним символом». Скан каждый раз писался руками
по новой — этот модуль кладёт ему место.

## Роль буквы зависит от роли файла

| Файл | Что проверяется | Находка |
|------|-----------------|---------|
| `.py` | строки вне docstring | кириллица (строки, доходящие до глаз, — английские) |
| `.py` | комментарии и docstring | только иероглифы: кириллица здесь — норма |
| `.html` | весь файл целиком, вплоть до `<style>` | кириллица и иероглифы |
| `.sql` | синтаксис (строки и комментарии сбиты `sql_split_masked`) | кириллица и иероглифы |
| `.sql`, `.md`, `.js`, `.css`, `.json`, `.yml` | весь текст | иероглифы — след вставки из чужого источника |

## ⚠ Мера — чужой алфавит, а не «любой байт выше 0x7F»

Тире, кавычки-лапки, `°`, `×` — типографика, а не язык: em dash в английской
строке — не нарушение, а фильтр по любому байту выше 0x7F выгнал бы их все.
Судятся только буквы чужих письменностей: блок кириллицы `U+0400–U+04FF` (весь,
как в `text_cyrillic`: `[а-я]` теряет `ё`) и каны/хани/хангыль вместе с
CJK-пунктуацией и полноширинной ASCII-формой.

## ⚠ `strings_ascii` судит не везде

Кириллица в строках — нарушение в проектах-панелях, где весь пользовательский
текст английский. Там, где русский текст и есть продукт (бот, рассылка),
строки судить нельзя — `strings_ascii=False`.

## Чего модуль НЕ делает

**Не судит идентификаторы** — в именах кириллицу ищет `variable_cyrillic`, ей
там и быть. **Не ищет опечатки и склейки** — это `tool_typo`; пересечение одно
(иероглифы в прозе), и оно осознанно: здесь судья — роль файла, а не текст.
**Не чинит.** Найденное правит человек: подсунутый иероглиф в русском слове
чинится стиранием, а русская строка в html — переводом, и то и другое требует
взгляда.
"""
import argparse
import ast
import bisect
import io
import re
import sys
import tokenize
from pathlib import Path

from file_.file_walk import file_walk
from sql_.sql_split import sql_split_masked
from text_.text_cyrillic import TEXT_CYRILLIC_RE

# Каны, хани, хангыль, CJK-пунктуация и полноширинная ASCII-форма — тот же
# набор, чем судит tool_typo: иероглиф в коде — всегда след вставки.
TEXT_CHARSET_CJK = re.compile('[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf'
                              '\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff'
                              '\uff01-\uff5e]')

# Строка, целиком собранная из чужой письменности и дефисов — это определение
# класса букв (диапазоны, перечисленные прямо в регулярке), а не текст,
# доходящий до глаз.
TEXT_CHARSET_CLASSISH = re.compile(r'^[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf'
                                  r'\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff'
                                  r'\uff01-\uff5e\^\]\[-]+$')
TEXT_CHARSET_MD_CODE = re.compile(r'`[^`\n]*`|```[\s\S]*?```')

TEXT_CHARSET_PY = ('.py',)
TEXT_CHARSET_HTML = ('.html', '.htm')
TEXT_CHARSET_SQL = ('.sql',)
TEXT_CHARSET_FLAT = ('.md', '.js', '.css', '.json', '.yml', '.yaml', '.txt')
TEXT_CHARSET_ALL = TEXT_CHARSET_PY + TEXT_CHARSET_HTML + TEXT_CHARSET_SQL + TEXT_CHARSET_FLAT


def text_charset(root: str = '.', strings_ascii: bool = True) -> list[dict]:
    """Найти чужие алфавиты там, где их быть не должно.

    Args:
        root: каталог-корень; обход с пропусками `file_walk`.
        strings_ascii: судить кириллицу в питоновских строках вне docstring;
            `False` — для проектов, где русский текст и есть продукт.

    Returns:
        Список находок `{'file', 'line', 'col', 'kind', 'snippet'}`; `kind` —
        `cjk` | `cyrillic` | `cyrillic-string`. Пустой список — алфавит чист.

    ⚠⚠ Строки судятся деревом, а не текстом: кириллица в `docstring` — норма,
    та же кириллица в `raise ValueError('нет данных')` — нарушение, и глазами
    по файлу этих двух не развести. Отсюда и синтаксическая ошибка в файле —
    молчание: судить парсеру нечего, а кричать не о чём.

    ⚠ Колонка питоновской строки — начало литерала, а не самого символа:
    у f-строки и строки из нескольких частей внутренний офсет AST не честнее.
    """
    findings: list[dict] = []
    for path in file_walk(root, suffixes=TEXT_CHARSET_ALL):
        try:
            text = Path(path).read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        suffix = Path(path).suffix.lower()
        if suffix in TEXT_CHARSET_PY:
            findings += _python(path, text, strings_ascii)
        elif suffix in TEXT_CHARSET_HTML:
            findings += _raw(path, text, cyrillic=True)
        elif suffix in TEXT_CHARSET_SQL:
            findings += _raw(path, text, cyrillic=False)
            # Снаружи строк и комментариев остаются только имена и ключевые
            # слова: кириллица в них — это уже чужой синтаксис, а не текст.
            findings += _raw(path, sql_split_masked(text), cyrillic=True, kinds=('cyrillic',))
        elif suffix == '.md':
            # Код в обратных кавычках и блоки-заборы — цитаты, а не проза:
            # документ, описывающий класс иероглифов, не нарушает его же.
            findings += _raw(path, TEXT_CHARSET_MD_CODE.sub(lambda m: ' ' * len(m.group()), text),
                             cyrillic=False)
        else:
            findings += _raw(path, text, cyrillic=False)
    return findings


# ── детали реализации ─────────────────────────────────────────────────────────

def _python(path: str, text: str, strings_ascii: bool) -> list[dict]:
    """Строки вне docstring — глазами AST; комментарии — глазами токенизатора."""
    out: list[dict] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out  # непарсящийся файл судит не этот модуль
    docs = set()
    doc_nodes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, 'body', ())
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
                doc_nodes.append(body[0].value)
    # Docstring — проза: кириллица в ней норма, но иероглиф — след и там.
    for node in doc_nodes:
        found = TEXT_CHARSET_CJK.search(node.value)
        if found:
            out.append({'file': path, 'line': node.lineno, 'col': node.col_offset, 'kind': 'cjk',
                        'snippet': node.value[max(0, found.start() - 8):found.start() + 8]})
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            # Одна находка на литерал: находка про строку, а не про букву —
            # иначе сообщение тонет в повторе одного и того же окна.
            classish = bool(TEXT_CHARSET_CLASSISH.fullmatch(node.value))
            for pattern, kind in ((TEXT_CHARSET_CJK, 'cjk'), (TEXT_CYRILLIC_RE, 'cyrillic-string')):
                if kind == 'cyrillic-string' and not strings_ascii:
                    continue
                if classish and kind == 'cjk':
                    continue  # определение класса букв, не текст
                found = pattern.search(node.value)
                if found:
                    out.append({'file': path, 'line': node.lineno, 'col': node.col_offset, 'kind': kind,
                                'snippet': node.value[max(0, found.start() - 8):found.start() + 8]})
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            for found in TEXT_CHARSET_CJK.finditer(token.string):
                out.append({'file': path, 'line': token.start[0], 'col': token.start[1],
                            'kind': 'cjk',
                            'snippet': token.string[max(0, found.start() - 8):found.start() + 8]})
    return out


def _raw(path: str, text: str, cyrillic: bool, kinds: tuple = ()) -> list[dict]:
    """Кириллица/иероглифы по тексту как он есть, с номерами строк исходника."""
    out, seen = [], set()
    # Маска из sql_split_masked сохраняет длины и переводы строк, поэтому
    # позиции в ней переводятся в исходные номера без разбора.
    break_points = [m.start() for m in re.finditer('\n', text)]
    checks = [(TEXT_CHARSET_CJK, 'cjk')]
    if cyrillic:
        checks.append((TEXT_CYRILLIC_RE, kinds[0] if kinds else 'cyrillic'))
    for pattern, kind in checks:
        for found in pattern.finditer(text):
            line, _ = _pos(break_points, found.start())
            if (line, kind) in seen:
                continue  # находка про строку, а не про букву
            seen.add((line, kind))
            col = found.start() - (break_points[line - 1] + 1 if line else 0)
            out.append({'file': path, 'line': line, 'col': col, 'kind': kind,
                        'snippet': text[max(0, found.start() - 8):found.start() + 8]})
    return out


def _pos(break_points: list, offset: int) -> tuple:
    """Офсет — в (строка, колонка): бинарный поиск по переводам строк."""
    line = bisect.bisect_right(break_points, offset)
    start = break_points[line - 1] + 1 if line else 0
    return line + 1, offset - start


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Алфавит проекта: где завелись чужие буквы.')
    parser.add_argument('root', nargs='?', default='.', help='каталог-корень')
    parser.add_argument('--no-strings', dest='strings', action='store_false',
                        help='не судить кириллицу в строках (проект с русским текстом)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    hits = text_charset(args.root, strings_ascii=args.strings)
    if args.json:
        import json
        print(json.dumps(hits, ensure_ascii=False, indent=2))
    elif not hits:
        print('чисто')
    else:
        for hit in hits:
            print(f"{hit['file']}:{hit['line']}:{hit['col']} {hit['kind']} «{hit['snippet']}»")
    sys.exit(1 if hits else 0)

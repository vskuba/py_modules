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

С тех пор как плагины и веб-оболочки переехали на `.js`/`.mjs`, той же прозы
в них стало не меньше, чем в докстрингах, — комментарий «плагин хоста: домен
живёт в storages» пишется по-русски и в `.mjs`. Синтаксического дерева JS у
stdlib нет, поэтому комментарии вырезаются эвристикой: блок `/* */` целиком,
строка `//` — если слева кавычки сведены и нет признака адреса.

Настоящая вычитка — дело человека или языковой модели: `--prose` отдаёт всю прозу
репозитория одним потоком, готовым к отправке (например, `| gx10 "найди опечатки"`).

Отдельный режим `--cyrillic` — уже не проза, а код: дерево `*.py`, и в нём имена
функций и ключи словарей на кириллице, чего правило (`code_rules.md` §1.2) не терпит.
"""
import ast
import re
import sys

from pathlib import Path

# Файл запускают и путём (`python3 py_modules/tool_/tool_typo.py`). Тогда первым в путях
# лежит каталог файла, и соседний namespace (`file_`) не находится вовсе.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_.file_walk import file_walk

TOOL_TYPO_ROOT = Path(__file__).resolve().parents[1]

TOOL_TYPO_CYRILLIC = re.compile(r'[а-яёА-ЯЁ]')
TOOL_TYPO_LATIN = re.compile(r'[a-zA-Z]')

# Методы словаря, у которых первый довод — ключ: `row.get('ссылка')`.
#
# ⚠ `get` сюда входит, и это даёт редкую ложь: `client.get('/путь')` у HTTP-клиента
# — не ключ. Цена принята: кириллический путь в URL куда реже кириллического ключа,
# а различить их статически нечем — тип принимающего неизвестен.
TOOL_TYPO_KEY_CALLS = ('get', 'pop', 'setdefault')

# Пометка «ключ чужой, переименовать нельзя» и причина за ней.
#
# ⚠⚠ Причина обязательна (`.+` непустая): пометка без слов находкой **остаётся**.
# `# cyrillic-key:` без объяснения — это «замолчи», а не «вот владелец имени», и
# через полгода её не отличить от забытой.
TOOL_TYPO_KEY_ALLOW = re.compile(r'#\s*cyrillic-key:\s*(?P<why>.*)$')
TOOL_TYPO_LETTERS = re.compile(r'[^a-zA-Zа-яёА-ЯЁ]+')
# Иероглифика и полноширинные знаки: в кириллической прозе им места нет —
# это след вставки из чужого источника (машинный перевод, азиатская докс),
# глазами на кириллическом фоне они слипаются в «ещё один квадратик».
# Каны, хани, хангыль, CJK-пунктуация и полноширинная ASCII-форма.
TOOL_TYPO_CJK = re.compile('[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf'
                           '\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff'
                           '\uff01-\uff5e]')
# Повтор подряд — след правки. Промежуток между словами: пробелы и не более
# одного перевода строки; **пустой строки между повторами быть не может** —
# заголовок и первое слово абзаца под одним словом это норма документа
# («…когда есть сюита» / «Сюита проверяет»), а не слипшаяся правка.
TOOL_TYPO_DOUBLED = re.compile(r'\b([а-яё]{3,})[ \t]*(?:\n[ \t]*)?\1\b', re.IGNORECASE)
TOOL_TYPO_FENCE = re.compile(r'```.*?```', re.DOTALL)
TOOL_TYPO_INLINE = re.compile(r'`[^`\n]*`')
TOOL_TYPO_URL = re.compile(r'https?://\S+')

# JS-семейство: той же прозы в плагинах не меньше, чем в докстрингах.
TOOL_TYPO_JS = ('.js', '.mjs', '.cjs')

# Блок `/* ... */` целиком, лениво: жадный дочитал бы до конца файла.
TOOL_TYPO_JS_BLOCK = re.compile(r'/\*.*?\*/', re.DOTALL)

# Строковый комментарий: `//` не вплотную после двоеточия — иначе это коса
# адреса, а адрес до нас выбросит уже `_clean`.
TOOL_TYPO_JS_LINE = re.compile(r'(?<!:)//')

# Английское слово с русским окончанием — приём этого репозитория, а не ошибка:
# «JOINы», «switchать». Ошибка выглядит иначе — слипшимися словами или подменой буквы.
TOOL_TYPO_JARGON = re.compile(
    r'^[A-Za-z]{3,}(ы|ов|ам|ами|ах|а|у|е|ать|ают|ает|ал|ила|ится|ов)$')


def tool_typo(root=None) -> list[dict]:
    """Проверить текст на опечатки: склейки, подмены букв, повторы слов, иероглифы.

    Смотрит докстринги и комментарии `*.py`, комментарии `*.js`/`*.mjs`/`*.cjs`
    плюс `*.md`; код в обратных кавычках, блоки-заборы и адреса вырезаются —
    там своя орфография.

    Args:
        root: корень репозитория; пусто — тот, где лежит этот файл.

    Returns:
        list[dict]: находки с полями `kind` (`mixed`, `doubled` или `cjk`),
        `word`, `hint`, `where`, `line`.

    ⚠ Орфографию **не проверяет** и проверить не может: словаря нет. Чистый вывод
    означает «механических следов правки нет», а не «текст без ошибок» — за
    смыслом и согласованием идут к `--prose` и живому читателю.

    ⚠ Английское слово с русским окончанием (`JOINы`, `switchать`) — приём этого
    репозитория, из выдачи оно отфильтровано.
    """
    base = Path(root) if root else TOOL_TYPO_ROOT
    prose = _prose(base)
    return _mixed(prose) + _doubled(prose) + _cjk(prose)


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

    Смотрит `*.py` синтаксическим деревом: имена функций, классов, переменных
    и параметров, ключи литералов-словарей, строковые ключи в скобках
    (`словарь['ключ']`) и те же ключи у методов чтения (`row.get('ключ')`,
    `pop`, `setdefault`). Что не дерево (js, html, sql) деревом не видно — там
    остаются grep-шаблоны §1.2.

    Args:
        root: корень репозитория; пусто — тот, где лежит этот файл.

    Returns:
        list[dict]: находки с полями `kind` (`cyrillic`), `word`, `where`,
        `line`, `hint`.

    ⚠ Ловит и смешанные имена (`test_metadata_строкой`), а не только целиком
    кирилличные: их набираешь руками с той же раскладки, что и всё вокруг.

    ⚠ **Односимвольный ключ — буква, а не имя**, и он пропускается: таблица
    транслитерации (`i18n_`) законно ключуется буквами алфавита, и без этого
    послабления проверка выдавала 97 находок на одном словаре — то есть не
    выдавала ничего, потому что её переставали читать.

    ## ⚠⚠ Чужой ключ: пометка с причиной

    Имя поля бывает задано **не нами** — шагом `extract` в сценарии, схемой или
    протоколом на той стороне, — и переименование в одиночку разъехалось бы с ними
    молча. Такое место помечается в той же строке:

        link = row.get('ссылка')  # cyrillic-key: шаг extract сценария соседа

    У словаря пометка действует на весь словарь, если стоит в строке открывающей
    скобки или над ней: у таблицы на тридцать ключей причина одна на всех, и
    повторённая тридцать раз она стала бы шумом.

    ⚠ Причина **обязательна**: пустая пометка остаётся находкой (см.
    `TOOL_TYPO_KEY_ALLOW`).

    ## ⚠ Чего здесь нет: `in` и `not in`

    `'ключ' in row` — тоже чтение ключа, но тем же `in` проверяют **подстроку**
    (`'отказ' in str(error)`). Для дерева это один и тот же `Compare` со строкой
    слева, и различить их нечем: тип правого довода статически неизвестен. В этих
    проектах подстрочных проверок кратно больше, чем ключей, — ловля `in` дала бы
    проверку, которая ошибается чаще, чем находит, а такую снимают через неделю.
    """
    base = Path(root) if root else TOOL_TYPO_ROOT
    out = []
    for path in file_walk(base, ('.py',)):
        try:
            src = path.read_text(encoding='utf-8')
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        lines = src.split('\n')
        where = path.relative_to(base).as_posix()
        for node in ast.walk(tree):
            # ⚠ Строка находки у ключа — строка **самого ключа**, а не словаря:
            # иначе у словаря на тридцать ключей все находки пришлись бы на одну
            # строку, и чинить их было бы не по чему. Поэтому `names` несёт
            # четвёркой ещё и номер строки, и строку владельца для пометки.
            own = getattr(node, 'lineno', 1)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [(node.name, 'имя функции — по правилу только латиницей',
                          own, 0)]
            elif isinstance(node, ast.ClassDef):
                names = [(node.name, 'имя класса — по правилу только латиницей',
                          own, 0)]
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names = [(node.id, 'имя переменной — по правилу только латиницей',
                          own, 0)]
            elif isinstance(node, ast.arg):
                names = [(node.arg, 'имя параметра — по правилу только латиницей',
                          own, 0)]
            elif isinstance(node, ast.Dict):
                names = [(k.value, 'ключ словаря на кириллице', k.lineno, own)
                         for k in node.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            elif (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                names = [(node.slice.value, 'ключ словаря на кириллице',
                          node.slice.lineno, 0)]
            elif (isinstance(node, ast.Call)
                    and getattr(node.func, 'attr', '') in TOOL_TYPO_KEY_CALLS
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                names = [(node.args[0].value, 'ключ словаря на кириллице',
                          node.args[0].lineno, 0)]
            else:
                continue
            for name, tip, line, owns in names:
                # Один символ — буква алфавита в таблице перевода, не имя (см. ⚠).
                if len(name) < 2 or not TOOL_TYPO_CYRILLIC.search(name):
                    continue

                said = _key_allowed(lines, line, owns)
                if said and said.group('why').strip():
                    continue

                out.append({'kind': 'cyrillic', 'word': name[:60], 'where': where,
                            'line': line,
                            'hint': ('пометка `cyrillic-key:` без причины — '
                                     'она не считается') if said else tip})
    return sorted(out, key=lambda r: (r['where'], r['line'], r['word']))


def main() -> int:
    """CLI: `python -m tool_.tool_typo [--kind mixed|doubled|cjk] [--prose] [--cyrillic]`.

    Код возврата — число находок (0 — чисто, потолок 125): проверку вешают
    хуком перед коммитом, а хук читает код, а не глазами вывод. Потолок нужен
    потому, что код возврата живёт в байте — ровно 256 находок иначе выглядели
    бы успехом.
    """
    import argparse

    ap = argparse.ArgumentParser(
        description='Следы неудачной правки в прозе репозитория: докстринги и '
                    'комментарии `*.py`, комментарии `*.js`/`*.mjs`/`*.cjs` и '
                    '`*.md` — слипшиеся слова, подмена буквы из другого '
                    'алфавита, повтор слова, иероглифы из чужой вставки. Плюс — '
                    'кириллица в именах функций и ключах (--cyrillic).')
    ap.add_argument('--kind', default='',
                    choices=['', 'mixed', 'doubled', 'cjk', 'cyrillic'])
    ap.add_argument('--prose', action='store_true',
                    help='не искать, а выдать всю прозу для вычитки моделью')
    ap.add_argument('--cyrillic', action='store_true',
                    help='имена функций и ключи словарей на кириллице в *.py')
    ns = ap.parse_args()

    if ns.prose:
        print(tool_typo_prose())
        return 0

    if ns.cyrillic or ns.kind == 'cyrillic':
        rows = tool_typo_cyrillic()
        print(f'🔴 ИМЕНА И КЛЮЧИ НА КИРИЛЛИЦЕ — {len(rows)}' if rows
              else 'кириллицы в именах и ключах нет')
        for row in rows:
            print(f"  {row['where']}:{row['line']}  «{row['word']}»  {row['hint']}")
        return min(len(rows), 125)

    found = [f for f in tool_typo() if not ns.kind or f['kind'] == ns.kind]
    if not found:
        print('механических следов правки нет '
              '(орфографию это не проверяет — см. --prose)')
        return 0

    titles = {'mixed': '🔤 БУКВЫ ИЗ РАЗНЫХ АЛФАВИТОВ ИЛИ СЛИПШИЕСЯ СЛОВА',
              'doubled': '👯 СЛОВО ПОВТОРЕНО ДВАЖДЫ',
              'cjk': '🈴 ИЕРОГЛИФЫ — СЛЕД ВСТАВКИ ИЗ ДРУГОГО ИСТОЧНИКА'}

    for kind, title in titles.items():
        rows = [f for f in found if f['kind'] == kind]
        if not rows:
            continue
        print(f'\n{title} — {len(rows)}')
        for row in rows:
            print(f"  {row['where']}:{row['line']}  «{row['word']}»  {row['hint']}")

    return min(len(found), 125)


def _key_allowed(lines: list, line: int, owns: int):
    """Пометка `cyrillic-key:` для ключа. `None` — её нет.

    Ищется в трёх строках: самого ключа, открывающей скобки словаря (`owns`) и
    **над** ней. Третья — потому что комментарий перед определением это основной
    способ комментировать в питоне; у таблицы на тридцать ключей причина одна, и
    повторённая тридцать раз она превратилась бы в шум.

    ⚠ `owns` равен нулю там, где владельца нет (имя, индекс, вызов): тогда
    смотрится только строка самого ключа.
    """
    for at in (line, owns, owns - 1 if owns else 0):
        if 0 < at <= len(lines):
            said = TOOL_TYPO_KEY_ALLOW.search(lines[at - 1])
            if said:
                return said

    return None


def _prose(base: Path) -> list[tuple]:
    """Проза репозитория: (файл, строка, текст) из докстрингов, комментариев
    py и js-семейства плюс `*.md`."""
    out = []

    for path in file_walk(base, ('.py',)):
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

    for path in file_walk(base, ('.md',)):
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        where = path.relative_to(base).as_posix()
        for number, line in enumerate(_clean(text).splitlines(), 1):
            if line.strip():
                out.append((where, number, line))

    for path in file_walk(base, TOOL_TYPO_JS):
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        out.extend(_js_prose(path.relative_to(base).as_posix(), text))

    return out


def _js_prose(where: str, source: str) -> list[tuple]:
    """Комментарии js-семейства: блок `/* */` целиком, строка `//` — с охраной кавычек.

    Дерева у stdlib нет, поэтому охрана эвристическая и грубая: левее `//`
    должно быть чётное число каждой из кавычек (`"`, `'`, `` ` ``) — значит
    литерал к этому месту закрылся; `//` вплотную после `:` — коса адреса.

    ⚠ Регулярное выражение с заэкранированным слэшем внутри — не комментарий,
    и эвристика его не отличит: такие строки редки, а пропущенный комментарий
    дешевле ложного.
    """
    out = []
    for match in TOOL_TYPO_JS_BLOCK.finditer(source):
        start = source.count('\n', 0, match.start()) + 1
        # JSDoc-строки живут со «звёздочной» канаветой — снимаем её построчно,
        # и номер ставим настоящий, а не начало всего блока.
        for offset, row in enumerate(match.group()[2:-2].splitlines()):
            row = row.strip().lstrip('*').strip()
            if row:
                out.append((where, start + offset, _clean(row)))
    text = TOOL_TYPO_JS_BLOCK.sub(lambda m: '\n' * m.group().count('\n'), source)
    for number, line in enumerate(text.splitlines(), 1):
        match = TOOL_TYPO_JS_LINE.search(line)
        if not match:
            continue
        left = line[:match.start()]
        if (left.count('"') % 2 or left.count("'") % 2
                or left.count('`') % 2):
            continue
        body = line[match.end():].strip(' *\n')
        if body:
            out.append((where, number, _clean(body)))
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


def _cjk(prose: list[tuple]) -> list[dict]:
    """Иероглифы и полноширинные знаки в прозе — след вставки из чужого источника."""
    out = []
    for where, line, text in prose:
        for found in TOOL_TYPO_CJK.finditer(text):
            lo, hi = max(0, found.start() - 8), min(len(text), found.end() + 8)
            out.append({'kind': 'cjk', 'word': text[lo:hi].strip(),
                        'where': where, 'line': line,
                        'hint': 'иероглиф или полноширинный знак — след вставки'})
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
    raise SystemExit(main())

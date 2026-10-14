"""CSS как код: доедет ли он до браузера целиком и где потерялся, если нет.

Сосед — `js_check`: тот про браузерный разбор JS, этот про браузерный разбор CSS.
Нужда в отдельном инструменте та же и по той же причине: **у CSS нет исключений**.
Сломанный файл не падает — парсер браузера выбрасывает то, чего не понял, и идёт
дальше. Ни ошибки в консоли, ни записи в журнале, ни строчки в devtools: правило
просто не применилось.

Измеренный случай, ради которого модуль и написан. Текст комментария уехал за
пределы `/* … */`:

    ...правка в нём меняет все панели экосистемы. *​/
       ⚠ Потолка ширины нет: сетка городов... *​/
    .data-page { width: 100%; padding: 24px 20px; }

Парсер проглотил `.data-page` целиком. Страница встала уже экрана и без
прокрутки, `getComputedStyle` показывал `padding: 0px` при явно заданном
`24px 20px`, а искали неделю не там — в контейнере каркаса.

## ⚠ Что ищется, а что нет

Ищутся **поломки разбора**, после которых браузер теряет правила: незакрытый
комментарий, `*/` без начала, незакрытая или лишняя скобка. Это не линтер: ни
про неизвестные свойства, ни про порядок, ни про вендорные префиксы здесь нет
ни слова — для них есть stylelint, и он требует node и конфига.

Проверка своя, а не чужой библиотекой, ровно поэтому: одна забота, ноль
зависимостей, работает там же, где и всё остальное в слое.

## ⚠⚠ Строки и комментарии разбираются одним проходом

Не по очереди: `content: "/*"` — валидная строка, а `/* "незакрытая кавычка */`
— валидный комментарий. Разбери их двумя проходами, и каждый соврёт на входе
другого. Те же грабли описаны у `sql_split` (`docs/sql_split.md`) — там копия,
не умевшая комментариев, разошлась с умеющей на 136 файлах миграций из 232.
"""
import argparse
from pathlib import Path

# Сколько знаков исходника показать в месте поломки: строку видно целиком, а
# простыню минифицированного файла в вывод не тащим.
CSS_CHECK_CHUNK = 120


def css_check(path) -> dict:
    """Доедет ли CSS до браузера целиком; где он теряется, если нет.

    Args:
        path: путь к `.css`.

    Returns:
        {'intact': bool, 'what': 'что сломано' или '', 'where': 'файл:строка',
         'chunk': кусок исходника в месте поломки, 'rules': сколько правил
         разобралось}: у целого файла `what`/`where`/`chunk` пустые.

    ⚠ `rules` полезнее, чем кажется: файл бывает «целым» и почти пустым — если
    правил внезапно вдвое меньше, чем было, искать надо не здесь, а в том, что
    его собирает.
    """
    path = Path(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    found = _css_scan(text)

    if not found:
        return {'intact': True, 'what': '', 'where': '', 'chunk': '',
                'rules': _css_rules(text)}

    what, at = found

    return {'intact': False, 'what': what,
            'where': f'{path}:{1 + text.count(chr(10), 0, at)}',
            'chunk': _css_chunk(text, at), 'rules': _css_rules(text)}


def css_check_tree(root, pattern: str = '*.css') -> list:
    """Проверить все CSS дерева; вернуть **только сломанные**.

    Args:
        root: каталог обхода.
        pattern: маска файлов — сужается, когда в дереве лежит и чужая статика.

    Returns:
        список ответов `css_check` по сломанным файлам; пусто — все целы.
    """
    return [answer for file in sorted(Path(root).rglob(pattern))
            if not (answer := css_check(file))['intact']]


# ── Приватное ──────────────────────────────────────────────────────────────

def _css_scan(text: str):
    """Первая поломка разбора: `(что, смещение)`; целый файл — `None`.

    Один проход с тремя состояниями — комментарий, строка, глубина скобок.
    Порядок проверок внутри важен: комментарий сильнее строки (`/* " */` — это
    комментарий), строка сильнее скобок (`content: "}"` скобку не закрывает).
    """
    at, size, depth, opened = 0, len(text), 0, []

    while at < size:
        two = text[at:at + 2]

        if two == '/*':
            end = text.find('*/', at + 2)
            if end < 0:
                return 'комментарий не закрыт — всё до конца файла съедено', at
            at = end + 2
            continue

        if two == '*/':
            return ('«*/» без начала — комментарий закрыт раньше, и его хвост '
                    'читается как код: следующее правило браузер выбросит'), at

        char = text[at]
        if char in '"\'':
            at = _css_string_end(text, at)
            continue

        if char == '{':
            opened.append(at)
            depth += 1
        elif char == '}':
            if not depth:
                return 'лишняя «}» — блок закрыт дважды', at
            depth -= 1
            opened.pop()

        at += 1

    if depth:
        return f'«{{» не закрыта ({depth} шт.) — хвост файла внутри блока', opened[0]

    return None


def _css_string_end(text: str, at: int) -> int:
    """Смещение сразу за строковым литералом, начавшимся в `at`.

    ⚠ Незакрытая строка не ошибка этого модуля: браузер дочитывает её до конца
    строки и выбрасывает одно правило, а не файл. Отсюда и выход по `\\n` —
    иначе одна забытая кавычка объявляла бы сломанным весь хвост.
    """
    quote, at = text[at], at + 1
    while at < len(text):
        if text[at] == '\\':
            at += 2
            continue
        if text[at] in (quote, '\n'):
            return at + 1
        at += 1

    return at


def _css_rules(text: str) -> int:
    """Сколько блоков правил в файле — грубо, по открывающим скобкам вне строк."""
    at, size, count = 0, len(text), 0
    while at < size:
        two = text[at:at + 2]
        if two == '/*':
            end = text.find('*/', at + 2)
            at = size if end < 0 else end + 2
            continue
        if text[at] in '"\'':
            at = _css_string_end(text, at)
            continue
        count += text[at] == '{'
        at += 1

    return count


def _css_chunk(text: str, at: int) -> str:
    """Кусок исходника вокруг поломки — одной строкой, чтобы влез в вывод."""
    start = text.rfind('\n', 0, at) + 1
    end = text.find('\n', at)
    line = text[start:end if end > 0 else len(text)].strip()

    return line[:CSS_CHECK_CHUNK]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        prog='python3 -m css_.css_check',
        description='Доедет ли CSS до браузера целиком: комментарии и скобки.')
    parser.add_argument('path', help='файл .css или каталог обхода')
    parser.add_argument('--pattern', default='*.css',
                        help='маска файлов при обходе каталога')
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_dir():
        broken = css_check_tree(target, args.pattern)
        for row in broken:
            print(f'✗ {row["where"]} — {row["what"]}\n    {row["chunk"]}')
        print(f'сломано файлов: {len(broken)}' if broken else 'все CSS целы')
    else:
        answer = css_check(target)
        if answer['intact']:
            print(f'цел {target} · правил: {answer["rules"]}')
        else:
            print(f'✗ {answer["where"]} — {answer["what"]}\n    {answer["chunk"]}')

"""
Кто читает это имя: все места использования разом, а не grep по четырём слоям.

Удаление или переименование модуля оставляет строки в четырёх местах: python-код,
JS и шаблоны страниц, тесты, доки. Обход одним grep-ом одного слоя ловит два
слоя из четырёх — так удалённый модуль пережил удаление на строке в доке и в
тесте, которые никто не грепнул. Инструмент ищет имя по всем слоям сразу и
отвечает сгруппированно по ним: что править в этом же изменении.

О конкретном проекте не знает ничего: имя — любое, пути передаёт тот, кто зовёт.
"""
import argparse
import re
from pathlib import Path

TOOL_IMPACT_SKIP = ('.venv', '__pycache__', '.git', 'node_modules', '.idea')
# Порядок слоёв = порядок правки: сначала код, за ним разметка, тесты, доки.
TOOL_IMPACT_ORDER = ('код', 'разметка', 'тесты', 'док')
TOOL_IMPACT_SUFFIXES = ('.py', '.js', '.html', '.md')


def tool_impact(name: str, root='.') -> list[dict]:
    """Все места, где встречается имя: код, разметка, тесты, доки — сгруппированно.

    Ищет целое слово (границы слов): `photo_batch` не выстрелит внутри чего-то
    постороннего, а доки и тесты видны отдельными слоями — это строки, которые
    правят в том же изменении, что и код.

    Args:
        name: имя модуля, функции или ключа ответа — целое слово.
        root: откуда искать; обходит `.py`, `.js`, `.html`, `.md`.

    Returns:
        [{'file', 'layer', 'line', 'text'}, ...], слой за слоем: код → разметка
        → тесты → док; внутри слоя — по файлам и строкам.
    """
    word = re.compile(rf'(?<!\w){re.escape(name)}(?!\w)')
    base = Path(root)
    hits = []
    for path in sorted(base.rglob('*')):
        if not path.is_file() or path.suffix not in TOOL_IMPACT_SUFFIXES:
            continue
        if any(part in TOOL_IMPACT_SKIP for part in path.parts):
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        where = path.relative_to(base).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            if word.search(line):
                hits.append({'file': where, 'layer': _layer(where),
                             'line': number, 'text': line.strip()[:160]})
    hits.sort(key=lambda h: (TOOL_IMPACT_ORDER.index(h['layer']),
                             h['file'], h['line']))
    return hits


# ── детали реализации ──

def _layer(where: str) -> str:
    """Слой по пути: код → разметка → тесты → док — по порядку, а не по алфавиту."""
    if where.endswith(('.js', '.html')):
        return 'разметка'
    top = Path(where).parts[0] if Path(where).parts else ''
    if top in ('tests', 'test'):
        return 'тесты'
    if where.endswith('.md') or top in ('docs', 'doc'):
        return 'док'
    return 'код'


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Кто читает это имя по всем слоям сразу: код, разметка, '
                    'тесты, доки.')
    ap.add_argument('name', help='имя модуля, функции или ключа')
    ap.add_argument('--root', default='.', help='откуда искать')
    ap.add_argument('--layer', default='', choices=('',) + TOOL_IMPACT_ORDER,
                    help='только слой')
    ns = ap.parse_args()
    found = [h for h in tool_impact(ns.name, ns.root)
             if not ns.layer or h['layer'] == ns.layer]
    last = ''
    for h in found:
        if h['layer'] != last:
            print(f'\n— {h["layer"]}')
            last = h['layer']
        print(f'  {h["file"]}:{h["line"]}  {h["text"]}')
    print(f'\n{len(found)} мест')

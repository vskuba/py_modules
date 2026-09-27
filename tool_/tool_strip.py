"""
Имя вычёркивают из мест — и из строк, что его описывали: хвосты не молчат.

`tool_impact` отвечает, где имя стоит. Снять его — половина дела: над снятой
строкой остаётся комментарий, который о ней и говорил. Так правка «имя вышло
из подстановки» оставила трёхстрочный комментарий над уцелевшей строкой:
последняя строка комментария слово не содержала — и блок выжил наполовину, а
следующий читатель принял половину за живое пояснение к соседу.

Здесь исполнитель над находками `tool_impact`: строка, где имя было целой
записью (ключ словаря, аргумент, константа), снимается вместе с примкнувшим
сверху комментарием об имени; всё, где имя — часть уцелевающей строки или проза,
остаётся человеку и называется в отчёте. О проекте не знает ничего: имя и
корень передаёт тот, кто зовёт.

    PYTHONPATH=py_modules python -m tool_.tool_strip companion_id src/
    PYTHONPATH=py_modules python -m tool_.tool_strip companion_id src/ --apply
"""
import argparse
import re
import sys

from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_.tool_impact import tool_impact

# Строка, где имя — целая запись: ключ словаря (`'имя': ...`), именованный
# аргумент (`имя=...`), присваивание константы. Только такие строки снимаются
# целиком; прочее — уцелевающая строка с чужим именем, ей нужен человек.
TOOL_STRIP_ENTRY = 'запись — строка снимается'
TOOL_STRIP_BLOCK = 'хвост над снятой записью — снимается с ней'
TOOL_STRIP_PART = 'часть уцелевающей строки — правят руками'
TOOL_STRIP_WORD = 'слово в прозе — переписывают руками'


def tool_strip(name: str, root: str = '.', apply: bool = False) -> list[dict]:
    """Снять имя из мест, где оно было целой записью, и назвать хвосты.

    Args:
        name: имя, ключ или константа — целое слово.
        root: где правят; обход и слои — как у `tool_impact`.
        apply: True — вычёркивать записи и их блоки из файлов; пусто — только
            отчёт, правит тот, кто зовёт.

    Returns:
        list[dict]: `{file, line, action, text}` — где стояло и что с ним
        стало: снимается записью, снимается хвостом над записью, осталось
        частью строки или словом в прозе. Порядок — слой за слоем, как у
        `tool_impact`: код → разметка → тесты → доки.

    ⚠⚠ Снимается **целиком** строка-запись и весь непрерывный `#`-блок над
    ней, если имя упомянуто хоть в одной его строке: половина блока без
    записи-соседа читается живым пояснением к тому, что уцелело.
    """
    entry = re.compile(rf'^[\x27"]?{re.escape(name)}[\x27"]?\s*[:,=]')
    word = re.compile(rf'(?<!\w){re.escape(name)}(?!\w)')
    base = Path(root)
    out = []

    hits = tool_impact(name, root)
    for one_file in dict.fromkeys(one['file'] for one in hits):
        # одним проходом: сначала записи и их блоки (они помечают строки),
        # затем разбор каждой находки пометкой — иначе комментарий над
        # записью отчёт видел бы дважды: своим словом и чужим блоком.
        lines = (base / one_file).read_text(encoding='utf-8').splitlines()
        here = [h for h in hits if h['file'] == one_file]
        gone = set()
        for one in here:
            i = one['line'] - 1
            if i < len(lines) and entry.match(lines[i].strip()):
                gone |= {i} | _block(lines, i, word)

        for one in here:
            i = one['line'] - 1
            said = lines[i].strip() if i < len(lines) else ''
            if i in gone:
                action = (TOOL_STRIP_ENTRY if entry.match(said)
                          else TOOL_STRIP_BLOCK)
            elif said.startswith('#') or one['layer'] != 'код':
                action = TOOL_STRIP_WORD
            else:
                action = TOOL_STRIP_PART
            out.append({'file': one_file, 'line': one['line'],
                        'action': action, 'text': said[:160]})

        if apply:
            _cut(base / one_file, gone)

    return out


def tool_strip_format(found: list) -> str:
    """Отчёт словами: что снимается, что остаётся человеку."""
    if not found:
        return 'Мест не нашлось вовсе.'

    out = [f'Мест: {len(found)}.']
    file_now = ''
    for one in found:
        if one['file'] != file_now:
            file_now = one['file']
            out.append(f'\n── {file_now}')
        out.append(f"   {one['line']:>5}  {one['action']:28} {one['text']}")

    return '\n'.join(out)


# ── детали реализации ──

def _block(lines: list, at: int, word: re.Pattern) -> set:
    """Номера строк `#`-блока, примкнувшего сверху к строке `at`.

    Имя обязано стоять хоть в одной строке блока — иначе это пояснение к
    соседу, а не хвост записи, и снимать его нельзя.
    """
    start = at - 1
    if start < 0 or not lines[start].lstrip().startswith('#'):
        return set()
    while start > 0 and lines[start - 1].lstrip().startswith('#'):
        start -= 1
    block = set(range(start, at))
    return block if any(word.search(lines[b]) for b in block) else set()


def _cut(path: Path, nums: set) -> None:
    """Вычёркивание помеченных строк (номера 0-базные) из файла."""
    if not nums:
        return
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    path.write_text(''.join(l for i, l in enumerate(lines)
                            if i not in nums), encoding='utf-8')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Снять имя из мест, где оно было целой записью, '
                    'и назвать хвосты.')
    ap.add_argument('name', help='имя, ключ или константа')
    ap.add_argument('root', nargs='?', default='.', help='где правят')
    ap.add_argument('--apply', action='store_true',
                    help='вычёркивать записи и их блоки из файлов')
    ns = ap.parse_args()
    print(tool_strip_format(tool_strip(ns.name, ns.root, ns.apply)))

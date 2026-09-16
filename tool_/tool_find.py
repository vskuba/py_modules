"""
Найти инструмент по намерению: «хочу залогиниться» → чем это здесь делают.

Функций в общем слое больше четырёх сотен, общего реестра нет, и второй вариант уже
существующей функции пишется обычно не от лени — оттого, что первую не нашли. Вход сюда
один: сказать словами, что нужно, и получить короткий список кандидатов с путём и первой
строкой докстринга.

Поиск **лексический**, без эмбеддингов и без индекса на диске: опись собирается обходом
дерева за пару секунд (`tool_catalog`), а слова запроса сводятся со словами описи по
общему куску — так «логин» находит «автоперелогин», а «кадр» — «кадры». Морфологии это
не заменяет, поэтому у функции есть второй режим: когда запрос покрыт слабо, вместо
выдуманной выдачи печатается карта — namespace'ы и темы документации, по которым видно,
куда идти дальше.
"""
import re
import sys

from pathlib import Path

# Файл запускают и путём (`python3 py_modules/tool_/tool_find.py`). Тогда первым в путях
# лежит каталог файла, и `import tool_` находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_.tool_catalog import tool_catalog

TOOL_FIND_LIMIT = 12
TOOL_FIND_MIN_STEM = 5          # общий кусок, с которого слова считаются роднёй
TOOL_FIND_PREFIX = 0.85         # общее начало: «картинка» и «картинке» — одно слово
TOOL_FIND_FRAGMENT = 0.45       # кусок в середине: «логин» внутри «залогиниться» — может, случайность
TOOL_FIND_WEAK = 0.5            # качество совпадения, ниже которого выдача признаётся слабой

# Себя инструмент из выдачи убирает: «ищи инструмент искалкой инструментов» — не ответ,
# а слова примеров из его докстрингов иначе становятся корпусом и ловят чужие запросы.
TOOL_FIND_SELF = 'tool_/tool_find.py'
TOOL_FIND_WEIGHT = {'name': 5.0, 'summary': 3.0, 'where': 3.0, 'text': 1.0}
TOOL_FIND_SPLIT = re.compile(r'[^0-9a-zа-яё]+')

# Слова, которыми описывают намерение, а не предмет: в описи они не значат ничего.
TOOL_FIND_STOP = frozenset((
    'как', 'что', 'чем', 'где', 'для', 'это', 'или', 'без', 'над', 'под', 'при',
    'хочу', 'нужно', 'надо', 'мне', 'сделать', 'получить', 'py', 'the', 'and',
))


def tool_find(query: str, limit: int = TOOL_FIND_LIMIT, root=None) -> list[dict]:
    """Найти инструмент по намерению, сказанному словами.

    Args:
        query: что нужно сделать — «снять экран телефона», «залогиниться в панель».
        limit: сколько кандидатов вернуть.
        root: корень py_modules; пусто — тот, где лежит этот файл.

    Returns:
        list[dict]: записи описи (см. `tool_catalog`) с добавленными `score` и
        `coverage`. Порядок — по убыванию веса. Пусто — не нашлось ничего.

    ⚠ `coverage` важнее `score` и считается **по качеству**, а не по числу слов:
    точное совпадение даёт 1.0, общее начало — `TOOL_FIND_PREFIX`, кусок в
    середине слова — всего `TOOL_FIND_FRAGMENT`. Поэтому запрос, совпавший одним
    обрывком, честно проваливается ниже `TOOL_FIND_WEAK`, и CLI печатает карту
    вместо выдуманной выдачи.

    ⚠ Совпадение чисто лексическое. Синонимы без общего корня («залогиниться» и
    «вошедший клиент») не встретятся: для них и существует карта.
    """
    wanted = _tokens(query)
    if not wanted:
        return []

    records = [r for r in tool_catalog(root) if r['where'] != TOOL_FIND_SELF]
    vocab: dict[str, dict[int, float]] = {}

    for index, record in enumerate(records):
        for field, weight in TOOL_FIND_WEIGHT.items():
            for token in set(_tokens(str(record.get(field, '')))):
                owners = vocab.setdefault(token, {})
                if owners.get(index, 0.0) < weight:
                    owners[index] = weight

    score: dict[int, float] = {}
    covered: dict[int, dict] = {}

    for want in set(wanted):
        for token, owners in vocab.items():
            kin = _kinship(want, token)
            if not kin:
                continue
            for index, weight in owners.items():
                score[index] = score.get(index, 0.0) + weight * kin
                best = covered.setdefault(index, {})
                if best.get(want, 0.0) < kin:
                    best[want] = kin

    unique = len(set(wanted))
    coverage = {i: sum(best.values()) / unique for i, best in covered.items()}

    # Вес умножается на покрытие, а не складывается с ним. Иначе одно слово,
    # попавшее в имя функции (вес 5), перевешивает три слова, попавшие в описание
    # темы, — и лучший ответ уезжает вниз, что и случилось на «разовый запрос к базе».
    ranked = sorted(score, key=lambda i: (-score[i] * coverage[i], records[i]['where']))

    out = []
    for index in ranked[:limit]:
        record = dict(records[index])
        record['score'] = round(score[index], 2)
        record['coverage'] = round(coverage[index], 2)
        out.append(record)

    return out


def tool_find_map(root=None) -> dict:
    """Карта библиотеки — куда идти, когда поиск словами не помог.

    Args:
        root: корень py_modules; пусто — тот, где лежит этот файл.

    Returns:
        dict: `namespaces` — список `(namespace, сколько функций, сколько с CLI)`
        по убыванию размера; `topics` — список `(тема, о чём она)` из индекса
        документации.
    """
    records = tool_catalog(root)

    counts: dict[str, list] = {}
    for record in records:
        if record['kind'] != 'func':
            continue
        space = record['where'].split('/')[0]
        row = counts.setdefault(space, [0, 0])
        row[0] += 1
        row[1] += 1 if record['cli'] else 0

    namespaces = sorted(
        ((space, row[0], row[1]) for space, row in counts.items()),
        key=lambda item: (-item[1], item[0])
    )
    topics = [(r['name'], r['summary']) for r in records if r['kind'] == 'topic']

    return {'namespaces': namespaces, 'topics': topics}


def main() -> None:
    """CLI: `python -m tool_.tool_find «намерение» [--limit N] [--map]`."""
    import argparse

    ap = argparse.ArgumentParser(
        description='Найти инструмент py_modules по намерению, сказанному словами.')
    ap.add_argument('query', nargs='*', help='что нужно сделать, обычными словами')
    ap.add_argument('--limit', type=int, default=TOOL_FIND_LIMIT)
    ap.add_argument('--map', action='store_true',
                    help='не искать, а показать карту namespace и тем')
    ns = ap.parse_args()

    if ns.map or not ns.query:
        _map_print(tool_find_map())
        return

    query = ' '.join(ns.query)
    found = tool_find(query, limit=ns.limit)
    best = max((f['coverage'] for f in found), default=0.0)

    if found:
        _found_print(found)

    if best < TOOL_FIND_WEAK:
        print(f'\n⚠ Запрос «{query}» покрыт слабо'
              f' (качество совпадений {best:.0%}) — выдаче выше верить не стоит:'
              ' слова совпали обрывками, а не целиком.')
        print('  Возьмите namespace или тему с карты ниже и спросите ещё раз'
              ' теми словами, которыми описан модуль.\n')
        _map_print(tool_find_map())


def _tokens(text: str) -> list[str]:
    """Слова строки: нижний регистр, без коротких и без слов-пустышек."""
    return [word for word in TOOL_FIND_SPLIT.split(text.lower())
            if len(word) >= 3 and word not in TOOL_FIND_STOP]


def _kinship(want: str, token: str) -> float:
    """Родство двух слов: то же слово, одно начало или лишь общий кусок.

    Различать два последних обязательно. Общее начало — почти всегда то же слово в
    другой форме («картинка» и «картинке»). Кусок в середине — часто совпадение
    без смысла: «логин» сидит внутри «залогиниться» по делу, а внутри «каталогинг»
    уже случайно, и цена у такой находки должна быть низкой.
    """
    if want == token:
        return 1.0

    if len(want) < TOOL_FIND_MIN_STEM or len(token) < TOOL_FIND_MIN_STEM:
        return 0.0

    limit = min(len(want), len(token))
    same = 0
    while same < limit and want[same] == token[same]:
        same += 1

    if same >= TOOL_FIND_MIN_STEM:
        return TOOL_FIND_PREFIX

    return TOOL_FIND_FRAGMENT if (want in token or token in want) else 0.0


def _found_print(found: list[dict]) -> None:
    """Печать выдачи одним ранжированным списком.

    Функции и темы намеренно не разведены по разделам: раздел ломает порядок, и
    тема, покрывшая запрос на 95%, оказывается под функциями с 33%.
    """
    print()
    for item in found:
        if item['kind'] == 'func':
            head = f"🔧 {item['where']}:{item['name']}"
            head += '  [CLI]' if item['cli'] else ''
        else:
            head = f"📄 {item['where']}"

        print(f"  {head}  ({item['coverage']:.0%})")
        if item['summary']:
            print(f"      {item['summary']}")


def _map_print(chart: dict) -> None:
    """Печать карты: namespace со счётчиком и темы документации одной строкой."""
    print('📦 NAMESPACE (функций / из них модулей с CLI)')
    for space, total, cli in chart['namespaces']:
        print(f'  {space:<22} {total:>3} / {cli}')

    print('\n📄 ТЕМЫ')
    for name, about in chart['topics']:
        print(f'  {name:<22} {about[:96]}')


if __name__ == '__main__':
    main()

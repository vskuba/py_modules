"""
Порядок как факт кода: где в файле впервые встречается каждый из кусков.

Бывают места, где порядок строк — предметная правда, а не вкус: очередь,
которую исполнитель берёт сверху вниз, панель, которая обязана показывать виды в
порядке исполнителя. Такую правку тестом от дефекта отделяет один шаг —
проверка «литерал вида встречается раньше следующего». Руками это выглядело
как `тело.index(...)` посреди теста и ломалось на первой же кавычке.

Инструмент делает шаг одним вызовом: файл и список «что раньше чего» — ответ
«где что впервые встретилось» и вердикт. Порядок проверяется по ПОЯВЛЕНИЮ в
файле — не по работе программы: это про исходник, по которому правят.
"""
import argparse
from pathlib import Path


def tool_order(paths, items: list) -> dict:
    """Порядок кусков в файлах: где впервые встречается каждый, и сходится ли он с заказанным.

    Args:
        paths: файл или список файлов, где искать (литералы ищутся как текст).
        items: куски в порядке «раньше — позже» (литералы, имена, ключи).

    Returns:
        {'ok': bool, 'places': [{'item', 'file', 'line'}], 'wrong': [куски вне
        порядка]}: `places` — где каждый кусок встречается первым, `ok` — что
        строки идут в заказанном порядке. ⚠ Ищет текстом: если литерал стоит в
        докстринге выше места сборки, «первое вхождение» — именно докстринг;
        порядок тогда проверяют по месту сборки, а не по всему модулю.
    """
    files = [Path(p) for p in (paths if isinstance(paths, (list, tuple))
                               else [paths])]
    lines = []
    for f in files:
        for number, line in enumerate(f.read_text(encoding='utf-8')
                                      .splitlines(), 1):
            lines.append((f.name, number, line))
    places, missed = [], []
    for item in items:
        at = [i for i, (_, _, text) in enumerate(lines) if item in text]
        if at:
            i = at[0]
            places.append({'item': item, 'file': lines[i][0], 'line': lines[i][1],
                           'at': i})
        else:
            missed.append(item)
    ok = [p['at'] for p in places] == sorted(p['at'] for p in places)
    for p in places:
        p.pop('at')
    return {'ok': ok, 'places': places, 'missed': missed}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Порядок кусков в файле: где что впервые встречается и '
                    'сходится ли с заказанным.')
    ap.add_argument('paths', nargs='+', help='файлы, где искать')
    ap.add_argument('--then', action='append', required=True, metavar='КУСОК',
                    help='кусок; повторяют несколько раз — раньше, позже')
    ns = ap.parse_args()
    verdict = tool_order(ns.paths, ns.then)
    for p in verdict['places']:
        print(f'  {p["item"]!r}: {p["file"]}:{p["line"]}')
    for missed in verdict['missed']:
        print(f'  {missed!r}: НЕ НАЙДЕН')
    if verdict['ok'] and not verdict['missed']:
        print('порядок совпадает')
    else:
        raise SystemExit(2)

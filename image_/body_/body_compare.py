"""
Сверка паспортов фигур: два паспорта → оценка 0..1 с полевом пояснем.

Сверка не эмбеддингом, а полем за полем: «силуэт совпал, полнота соседняя» —
то, что человек читает глазами и оспаривает; косинус вектора таких слов не
объясняет. Поле, неопределимое хотя бы с одной стороны («не видно» или пусто),
из сверки выпадает, его вес перераспределяется на остальные — портрет не
наказывает кандидата за невидимые бёдра.
"""
import argparse
import json

from image_.body_.body_ import BODY_CORE_FIELDS, BODY_FRAME_FIELDS, BODY_ORDERED, \
    BODY_NOT_VISIBLE

# Веса полей в итоговой оценке; сумма — единица. Силуэт и полнота — то, за чем
# идут; мышца и плечи-бёдра — нюанс, их спор решают, но вердикт не делают.
BODY_COMPARE_WEIGHTS = {
    'silhouette': 0.30, 'volume': 0.20, 'build': 0.15, 'waist': 0.10,
    'shoulders_vs_hips': 0.10, 'legs_vs_torso': 0.10, 'muscle': 0.05,
}


def body_compare(a: dict, b: dict) -> dict:
    """Сверить два паспорта фигуры и вернуть оценку с полевом пояснем.

    Точное совпадение значения — весь вес поля, соседние значения упорядоченного
    ряда (BODY_ORDERED) — половина. Поля, невидимые хотя бы с одной стороны,
    из сверки выпадают, их вес перераспределяется на сравнимые.

    Args:
        a: паспорт источника (из `body_passport`)
        b: паспорт кандидата (из `body_passport`)

    Returns:
        success: всегда True, кроме case ошибки
        score: 0..1 по сравнимым полям; None — сверять нечего (оба без полей)
        fields: по каждому полю {field, a, b, points, weight} — что как сверилось
        caveats: сверяемые оговорки кадра, где кадры различаются (framing...)
        error: только при success=false
    """
    rows, used = [], 0.0
    for field, order in BODY_CORE_FIELDS.items():
        va, vb = a.get(field) or BODY_NOT_VISIBLE, b.get(field) or BODY_NOT_VISIBLE
        if BODY_NOT_VISIBLE in (va, vb):
            continue
        if va == vb:
            points = 1.0
        elif field in BODY_ORDERED and abs(order.index(va) - order.index(vb)) == 1:
            points = 0.5  # сосед по шкале: стройное~среднее — не спор, но и не совпадение
        else:
            points = 0.0
        weight = BODY_COMPARE_WEIGHTS[field]
        used += weight
        rows.append({'field': field, 'a': va, 'b': vb,
                     'points': points, 'weight': round(weight, 3)})
    caveats = {f: [a.get(f), b.get(f)]
               for f in BODY_FRAME_FIELDS if a.get(f) and b.get(f)
               and a.get(f) != b.get(f)}
    return {'success': True,
            'score': round(sum(r['weight'] * r['points'] for r in rows) / used, 4)
            if used else None,
            'fields': rows, 'caveats': caveats}


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Сверить два JSON-паспорта фигуры (файлы, что пишет body_).')
    p.add_argument('a', help='паспорт источника (json-файл)')
    p.add_argument('b', help='паспорт кандидата (json-файл)')
    ns = p.parse_args()
    load = lambda path: json.loads(open(path, encoding='utf-8').read())
    print(json.dumps(body_compare(load(ns.a), load(ns.b)),
                     ensure_ascii=False, indent=1))

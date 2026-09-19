"""Граф до очереди: возьмёт ли его ферма как есть и свежая ли копия у персоны.

Оба вопроса стоили смены по прогону каждый: граф, заведённый под flux, ехал с
полым кондом к SDXL-базе и валился после двух попыток; копия графа у персоны
пересоздавалась из полу-исправленного шаблона — и перестановки прогона лечили
не его, а только молчали, пока сверку не делал рукой. `comfy_graph_check`
отвечает structural: класс объявлен, required-входы поданы, линия из узла
даёт тип, который вход ждёт, файла в enumerate загрузчика нет — словами и ДО
отправки. Семантику (что патч — не юнет) проверка не ловит: для неё —
`comfy_.comfy_node`.

`comfy_graph_stale` — второй вопрос: копия шаблона у персоны сверяется с
шаблоном молча и по содержимому, а не по mtime.

Знания о конкретном проекте тут нет: графы и адреса приходят вызывающему;
разметка проекта (`_cls`, маркеры `__…__`) — оба формата (и API-`class_type`)
читаются одинаково.
"""
import json
import urllib.parse


def _узел(нода: dict) -> str:
    """Класс узла в обеих разметках: проект (`_cls`) или API (`class_type`)."""
    return нода.get('_cls') or нода.get('class_type') or ''


def _graph(граф: dict | str) -> dict:
    if isinstance(граф, str):
        with open(граф, encoding='utf-8') as f:
            return json.load(f)
    return граф


def comfy_graph_check(граф: dict | str, base: str = '') -> dict:
    """Возьмёт ли ферма граф как есть; словами о каждом узле, что не так.

    Args:
        граф: dict (разметка проекта `_cls`/`inputs` или API-`class_type`) или
            путь к json.
        base: адрес ComfyUI; пусто — `comfy_node`-ный дефолт.

    Returns:
        {'годен': bool, 'узлы': [{'узел', 'почему'}]} — проверки структурные:
        класс объявлен, required-вход подан, линия из узла даёт ждёмый тип,
        строковое имя файла видно в enumerate входа. Значения с маркером `__…__`
        (их подставит исполнитель) не сверяются.
    """
    from comfy_.comfy_node import comfy_node_info
    граф = _graph(граф)
    узлы = {к: в for к, в in граф.items() if isinstance(в, dict)}
    проблемы = []
    for к, нода in узлы.items():
        cls = _узел(нода)
        if not cls:
            проблемы.append({'узел': к, 'почему': 'узел без класса'})
            continue
        try:
            info = comfy_node_info(cls, base)
        except ValueError as e:
            проблемы.append({'узел': к, 'почему': str(e)})
            continue
        контракт = dict(info['обязательные'])
        контракт.update(info['необязательные'])
        вход_узла = нода.get('inputs', {})
        for имя in контракт:
            if имя not in вход_узла:
                проблемы.append({'узел': f'{к} ({cls})', 'почему':
                                f'не подан вход «{имя}»: нода ждёт '
                                f'{контракт[имя]["тип"]}'})
        for имя, значение in вход_узла.items():
            if имя not in контракт:
                проблемы.append({'узел': f'{к} ({cls})',
                                 'почему': f'у входа «{имя}» такой ноды нет'})
                continue
            ждём = ([контракт[имя]['тип']]
                    if контракт[имя]['тип'] != 'COMBO'
                    else контракт[имя].get('значения', []))
            if isinstance(значение, list) and значение:
                исток, idx = str(значение[0]), int(значение[1])
                if исток not in узлы:
                    проблемы.append({'узел': f'{к}.{имя}', 'почему':
                                    f'линия с узла «{исток}», а такого узла нет'})
                    continue
                выходы = _выходы(узлы[исток], base) if исток in узлы else []
                if not выходы or idx >= len(выходы):
                    проблемы.append({'узел': f'{к}.{имя}', 'почему':
                                    f'у «{исток}» нет выхода {idx}'})
                    continue
                дано = выходы[idx] if выходы[idx] else 'COMBO'
                if ждём and дано != 'COMBO' and дано not in ждём:
                    проблемы.append({'узел': f'{к}.{имя}', 'почему':
                                    f'вход ждёт {"/".join(map(str, ждём))}, '
                                    f'линия с «{исток}» даёт {дано}'})
            elif isinstance(значение, str) and '__' not in значение:
                enum = контракт[имя].get('значения') or []
                if enum and значение not in enum:
                    проблемы.append({'узел': f'{к}.{имя}', 'почему':
                                    f'файл «{значение}» ферма не видит; видит '
                                    f'{enum[:5]}{"…" if len(enum) > 5 else ""}'})
    return {'годен': not проблемы, 'узлы': проблемы}


def comfy_graph_stale(шаблон: dict | str, копия: dict | str) -> dict:
    """Свежая ли копия графа у персоны: чем именно разнится с шаблоном.

    Args:
        шаблон, копия: dict или путь к json.

    Returns:
        {'такой_же': bool, 'отличия': [{'узел', 'поле', 'в шаблоне',
        'в копии'}]} — узлы, заведённые лишь в одной из половин, тоже отличия
        ('в ...' пустое там, где узла нет).
    """
    шаблон, копия = _graph(шаблон), _graph(копия)
    отличия = []
    for к in sorted(set(шаблон) | set(копия)):
        if not (isinstance(шаблон.get(к), dict) or isinstance(копия.get(к), dict)):
            continue
        if к not in шаблон or к not in копия:
            отличия.append({'узел': к, 'поле': 'узел целиком',
                            'в шаблоне': 'есть' if к in шаблон else '',
                            'в копии': 'есть' if к in копия else ''})
            continue
        а, б = шаблон[к], копия[к]
        if _узел(а) != _узел(б):
            отличия.append({'узел': к, 'поле': 'класс',
                            'в шаблоне': _узел(а), 'в копии': _узел(б)})
        поля = set(а.get('inputs', {})) | set(б.get('inputs', {}))
        for поле in sorted(поля):
            в_шаблоне = а.get('inputs', {}).get(поле, '')
            в_копии = б.get('inputs', {}).get(поле, '')
            if в_шаблоне != в_копии:
                отличия.append({'узел': к, 'поле': поле,
                                'в шаблоне': в_шаблоне, 'в копии': в_копии})
    return {'такой_же': not отличия, 'отличия': отличия}


def _выходы(нода: dict, base: str) -> list:
    """Типы выходов узла с той же фермы (для сверки линии)."""
    from comfy_.comfy_node import comfy_node_info
    try:
        return comfy_node_info(_узел(нода), base)['выходы']
    except ValueError:
        return []


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='график перед очередью: возьмёт ли ферма граф как есть '
                    '(«проверить») и свежая ли копия у персоны («сверить»).')
    ap.add_argument('режим', choices=['проверить', 'сверить'])
    ap.add_argument('граф', help='путь к json графа (проверить) или копии')
    ap.add_argument('--шаблон', default='', help='путь к шаблону (сверить)')
    ap.add_argument('--base', default='', help='адрес ComfyUI')
    ns = ap.parse_args()
    if ns.режим == 'проверить':
        print(json.dumps(comfy_graph_check(ns.граф, ns.base),
                         ensure_ascii=False, indent=1))
    else:
        print(json.dumps(comfy_graph_stale(ns.шаблон, ns.граф),
                        ensure_ascii=False, indent=1))

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


def _node(node_: dict) -> str:
    """Класс узла в обеих разметках: проект (`_cls`) или API (`class_type`)."""
    return node_.get('_cls') or node_.get('class_type') or ''


def _graph(graph: dict | str) -> dict:
    if isinstance(graph, str):
        with open(graph, encoding='utf-8') as f:
            return json.load(f)
    return graph


def comfy_graph_check(graph: dict | str, base: str = '') -> dict:
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
    graph = _graph(graph)
    nodes_ = {k: v for k, v in graph.items() if isinstance(v, dict)}
    problems = []
    for k, node_ in nodes_.items():
        cls = _node(node_)
        if not cls:
            problems.append({'node': k, 'why': 'узел без класса'})
            continue
        try:
            info = comfy_node_info(cls, base)
        except ValueError as e:
            problems.append({'node': k, 'why': str(e)})
            continue
        contract = dict(info['required'])
        contract.update(info['optional'])
        node_inputs = node_.get('inputs', {})
        # ⚠ Отсутствие спрашивается только с ОБЯЗАТЕЛЬНЫХ входов. Необязательный
        # ферма подставляет сама, и требовать его значит кричать «не возьмёт»
        # там, где возьмёт: рабочие графы раздела не подают ни `device` у
        # DualCLIPLoader, ни `attn_mask` у ApplyPulidFlux — и прекрасно идут.
        # Инструмент, который ошибается на заведомо годном, перестают читать.
        # Тип по-прежнему сверяется у всех поданных — `контракт` ниже полный.
        for name in info['required']:
            if name not in node_inputs:
                problems.append({'node': f'{k} ({cls})', 'why':
                                f'не подан обязательный вход «{name}»: нода ждёт '
                                f'{contract[name]["type"]}'})
        for name, value in node_inputs.items():
            if name not in contract:
                problems.append({'node': f'{k} ({cls})',
                                 'why': f'у входа «{name}» такой ноды нет'})
                continue
            expect = ([contract[name]['type']]
                    if contract[name]['type'] != 'COMBO'
                    else contract[name].get('values', []))
            if isinstance(value, list) and value:
                source, idx = str(value[0]), int(value[1])
                if source not in nodes_:
                    problems.append({'node': f'{k}.{name}', 'why':
                                    f'линия с узла «{source}», а такого узла нет'})
                    continue
                outputs_ = _outputs(nodes_[source], base) if source in nodes_ else []
                if not outputs_ or idx >= len(outputs_):
                    problems.append({'node': f'{k}.{name}', 'why':
                                    f'у «{source}» нет выхода {idx}'})
                    continue
                given = outputs_[idx] if outputs_[idx] else 'COMBO'
                if expect and given != 'COMBO' and given not in expect:
                    problems.append({'node': f'{k}.{name}', 'why':
                                    f'вход ждёт {"/".join(map(str, expect))}, '
                                    f'линия с «{source}» даёт {given}'})
            elif isinstance(value, str) and '__' not in value:
                enum = contract[name].get('values') or []
                if enum and value not in enum:
                    problems.append({'node': f'{k}.{name}', 'why':
                                    f'файл «{value}» ферма не видит; видит '
                                    f'{enum[:5]}{"…" if len(enum) > 5 else ""}'})
    return {'fit': not problems, 'nodes': problems}


def comfy_graph_stale(template: dict | str, copy: dict | str) -> dict:
    """Свежая ли копия графа у персоны: чем именно разнится с шаблоном.

    Args:
        шаблон, копия: dict или путь к json.

    Returns:
        {'такой_же': bool, 'отличия': [{'узел', 'поле', 'в шаблоне',
        'в копии'}]} — узлы, заведённые лишь в одной из половин, тоже отличия
        ('в ...' пустое там, где узла нет).
    """
    template, copy = _graph(template), _graph(copy)
    diffs_ = []
    for k in sorted(set(template) | set(copy)):
        if not (isinstance(template.get(k), dict) or isinstance(copy.get(k), dict)):
            continue
        if k not in template or k not in copy:
            diffs_.append({'node': k, 'field': 'узел целиком',
                            'in_template': 'есть' if k in template else '',
                            'in_copy': 'есть' if k in copy else ''})
            continue
        a, b = template[k], copy[k]
        if _node(a) != _node(b):
            diffs_.append({'node': k, 'field': 'класс',
                            'in_template': _node(a), 'in_copy': _node(b)})
        fields = set(a.get('inputs', {})) | set(b.get('inputs', {}))
        for field_ in sorted(fields):
            in_template_ = a.get('inputs', {}).get(field_, '')
            in_copy_ = b.get('inputs', {}).get(field_, '')
            if in_template_ != in_copy_:
                diffs_.append({'node': k, 'field': field_,
                                'in_template': in_template_, 'in_copy': in_copy_})
    return {'same': not diffs_, 'diffs': diffs_}


def _outputs(node_: dict, base: str) -> list:
    """Типы выходов узла с той же фермы (для сверки линии)."""
    from comfy_.comfy_node import comfy_node_info
    try:
        return comfy_node_info(_node(node_), base)['outputs']
    except ValueError:
        return []


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='график перед очередью: возьмёт ли ферма граф как есть '
                    '(«check») и свежая ли копия у персоны («diff»).')
    ap.add_argument('mode', choices=['check', 'diff'])
    ap.add_argument('graph', help='путь к json графа (check) или копии')
    ap.add_argument('--template', default='', help='путь к шаблону (diff)')
    ap.add_argument('--base', default='', help='адрес ComfyUI')
    ns = ap.parse_args()
    if ns.mode == 'check':
        print(json.dumps(comfy_graph_check(ns.graph, ns.base),
                         ensure_ascii=False, indent=1))
    else:
        print(json.dumps(comfy_graph_stale(ns.template, ns.graph),
                        ensure_ascii=False, indent=1))

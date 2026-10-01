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


def comfy_graph_check(graph: dict | str, base: str = '') -> dict:
    """Возьмёт ли ферма граф как есть; словами о каждом узле, что не так.

    Args:
        graph: dict (разметка проекта `_cls`/`inputs` или API-`class_type`) или
            путь к json.
        base: адрес ComfyUI; пусто — `comfy_node`-ный дефолт.

    Returns:
        {'fit': bool, 'nodes': [{'node', 'why'}]} — проверки структурные:
        класс объявлен, required-вход подан, линия из узла даёт ждёмый тип,
        строковое имя файла видно в enumerate входа. Значения с маркером `__…__`
        (их подставит исполнитель) не сверяются.
    """
    from comfy_.comfy_node import comfy_node_info
    graph = _graph(graph)
    nodes = {key: value for key, value in graph.items() if isinstance(value, dict)}
    problems = []
    for key, node in nodes.items():
        cls = _node_class(node)
        if not cls:
            problems.append({'node': key, 'why': 'узел без класса'})
            continue
        try:
            info = comfy_node_info(cls, base)
        except ValueError as e:
            problems.append({'node': key, 'why': str(e)})
            continue
        contract = dict(info['required'])
        contract.update(info['optional'])
        given = node.get('inputs', {})
        # ⚠ Отсутствие спрашивается только с ОБЯЗАТЕЛЬНЫХ входов. Необязательный
        # ферма подставляет сама, и требовать его значит кричать «не возьмёт»
        # там, где возьмёт: рабочие графы раздела не подают ни `device` у
        # DualCLIPLoader, ни `attn_mask` у ApplyPulidFlux — и прекрасно идут.
        # Инструмент, который ошибается на заведомо годном, перестают читать.
        # Тип по-прежнему сверяется у всех поданных — `contract` ниже полный.
        for name in info['required']:
            if name not in given:
                problems.append({'node': f'{key} ({cls})', 'why':
                                f'не подан обязательный вход «{name}»: нода ждёт '
                                f'{contract[name]["type"]}'})
        for name, value in given.items():
            if name not in contract:
                problems.append({'node': f'{key} ({cls})',
                                 'why': f'у входа «{name}» такой ноды нет'})
                continue
            expected = ([contract[name]['type']]
                        if contract[name]['type'] != 'COMBO'
                        else contract[name].get('values', []))
            if isinstance(value, list) and value:
                src, idx = str(value[0]), int(value[1])
                if src not in nodes:
                    problems.append({'node': f'{key}.{name}', 'why':
                                    f'линия с узла «{src}», а такого узла нет'})
                    continue
                outs = _outputs(nodes[src], base) if src in nodes else []
                if not outs or idx >= len(outs):
                    problems.append({'node': f'{key}.{name}', 'why':
                                    f'у «{src}» нет выхода {idx}'})
                    continue
                got = outs[idx] if outs[idx] else 'COMBO'
                if expected and got != 'COMBO' and got not in expected:
                    problems.append({'node': f'{key}.{name}', 'why':
                                    f'вход ждёт {"/".join(map(str, expected))}, '
                                    f'линия с «{src}» даёт {got}'})
            elif isinstance(value, str) and '__' not in value:
                enum = contract[name].get('values') or []
                if enum and value not in enum:
                    problems.append({'node': f'{key}.{name}', 'why':
                                    f'файл «{value}» ферма не видит; видит '
                                    f'{enum[:5]}{"…" if len(enum) > 5 else ""}'})
    return {'fit': not problems, 'nodes': problems}


def comfy_graph_stale(template: dict | str, copy: dict | str) -> dict:
    """Свежая ли копия графа у персоны: чем именно разнится с шаблоном.

    Args:
        template, copy: dict или путь к json.

    Returns:
        {'same': bool, 'diffs': [{'node', 'field', 'in_template', 'in_copy'}]} —
        узлы, заведённые лишь в одной из половин, тоже отличия (`in_*` пустое
        там, где узла нет).
    """
    template, copy = _graph(template), _graph(copy)
    diffs = []
    for key in sorted(set(template) | set(copy)):
        if not (isinstance(template.get(key), dict) or isinstance(copy.get(key), dict)):
            continue
        if key not in template or key not in copy:
            diffs.append({'node': key, 'field': 'узел целиком',
                          'in_template': 'есть' if key in template else '',
                          'in_copy': 'есть' if key in copy else ''})
            continue
        node_t, node_c = template[key], copy[key]
        if _node_class(node_t) != _node_class(node_c):
            diffs.append({'node': key, 'field': 'класс',
                          'in_template': _node_class(node_t),
                          'in_copy': _node_class(node_c)})
        fields = set(node_t.get('inputs', {})) | set(node_c.get('inputs', {}))
        for field in sorted(fields):
            val_t = node_t.get('inputs', {}).get(field, '')
            val_c = node_c.get('inputs', {}).get(field, '')
            if val_t != val_c:
                diffs.append({'node': key, 'field': field,
                              'in_template': val_t, 'in_copy': val_c})
    return {'same': not diffs, 'diffs': diffs}


# ── детали реализации ──

def _node_class(node: dict) -> str:
    """Класс узла в обеих разметках: проект (`_cls`) или API (`class_type`)."""
    return node.get('_cls') or node.get('class_type') or ''


def _graph(graph: dict | str) -> dict:
    """Граф как есть либо прочитанный из json по пути."""
    if isinstance(graph, str):
        with open(graph, encoding='utf-8') as f:
            return json.load(f)
    return graph


def _outputs(node: dict, base: str) -> list:
    """Типы выходов узла с той же фермы (для сверки линии)."""
    from comfy_.comfy_node import comfy_node_info
    try:
        return comfy_node_info(_node_class(node), base)['outputs']
    except ValueError:
        return []


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='график перед очередью: возьмёт ли ферма граф как есть '
                    '(check) и свежая ли копия у персоны (compare).')
    ap.add_argument('mode', choices=['check', 'compare'])
    ap.add_argument('graph', help='путь к json графа (check) или копии')
    ap.add_argument('--template', default='', help='путь к шаблону (compare)')
    ap.add_argument('--base', default='', help='адрес ComfyUI')
    ns = ap.parse_args()
    if ns.mode == 'check':
        print(json.dumps(comfy_graph_check(ns.graph, ns.base),
                         ensure_ascii=False, indent=1))
    else:
        print(json.dumps(comfy_graph_stale(ns.template, ns.graph),
                         ensure_ascii=False, indent=1))

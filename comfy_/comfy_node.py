"""Контракты нод фермы: что нода требует, что отдаёт и какие файлы видит.

Граф, заведённый под одну базу, молча едет в очередь с кондой от другой: для
flux пустая конда работала, SDXL требует pooled-тензор — и прогон падает после
двух оседланных попыток словами `KSampler: 'pooled_output'`, а не до отправки.
Тот же класс вопросов к чужому графу без прогона: это юнет или патч (у патча
выход — MODEL_PATCH, и `UNETLoader` его не варит), видно ли имя файла в
enumerate загрузчика, какой тип ждёт нужный вход. Все ответы — в /object_info
фермы; здесь они одним вызовом вместо urllib-heredoc'а на каждый узел.

Знания о конкретном проекте тут нет: имя узла и адрес фермы приходят вызывающему.
"""
import json
import urllib.error
import urllib.request

# Ферма локальная по умолчанию — как в `comfy_gen`; чужой адрес даёт вызывающий.
COMFY_NODE_BASE = 'http://127.0.0.1:8188'


def comfy_node_info(name: str, base: str = '') -> dict:
    """Контракт ноды с фермы: входы с типами и enumerate-файлами, выходы.

    Args:
        name: имя класса (`SUPIRApply`, `CheckpointLoaderSimple`, …).
        base: адрес ComfyUI; пусто — `COMFY_NODE_BASE`.

    Returns:
        {'node': str, 'required': {имя: вход}, 'optional': {имя: вход},
        'outputs': [типы]}, где вход — {'type': str|[], 'values': [] (если
        enumerate-файл; иначе ''), 'default': что есть}. Для enum-входов
        'values' — тот самый список: видно, что файл виден загрузчику без
        прогона. `ValueError` словами — если такого узла на ферме нет: проверять
        граф чужой фермой молча означало бы соврать.
    """
    base = base or COMFY_NODE_BASE
    url = f'{base}/object_info/{urllib.request.quote(name)}'
    with urllib.request.urlopen(url, timeout=15) as r:
        body = json.load(r)
    node = body.get(name)
    if node is None:
        raise ValueError(f'узел «{name}» на ферме {base} не объявлен; '
                         'спроси имя из /object_info целиком')
    inputs = {'required': {}, 'optional': {}}
    for label in ('required', 'optional'):
        for field, v in (node.get('input', {}).get(label, {}) or {}).items():
            kind = v[0] if isinstance(v, list) and v else v
            extra = v[1] if isinstance(v, list) and len(v) > 1 and v[1] else {}
            entry = ({'type': kind, 'values': []} if isinstance(kind, str)
                     else {'type': 'COMBO', 'values': kind})
            if 'default' in extra:
                entry['default'] = extra['default']
            inputs[label][field] = entry
    return {'node': name, 'outputs': node.get('output', []), **inputs}


def comfy_node_sees(name: str, file: str, base: str = '') -> dict:
    """Видит ли загрузчик этот файл (имя в enumerate его входов) — без прогона.

    Args:
        name: имя класса ноды-загрузчика.
        file: имя файла, как его пишут во входе графа.
        base: адрес ComfyUI; пусто — `COMFY_NODE_BASE`.

    Returns:
        {'visible': bool, 'input': имя входа, 'values': длина enumerate} —
        видно, где именно имя нашлось; не нашлось — `input` пуст.
    """
    info = comfy_node_info(name, base)
    for label in ('required', 'optional'):
        for field, entry in info[label].items():
            if file in entry.get('values', []):
                return {'visible': True, 'input': field,
                        'values': len(entry['values'])}
    return {'visible': False, 'input': '', 'values': 0}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='контракт ноды с фермы: входы с типами и файлами, выходы.')
    ap.add_argument('node', help='класс ноды (`SUPIRApply`, …)')
    ap.add_argument('--base', default='', help='адрес ComfyUI (пусто — локальная)')
    ap.add_argument('--sees', default='', help='файл: видит ли его загрузчик')
    ns = ap.parse_args()
    if ns.sees:
        print(json.dumps(comfy_node_sees(ns.node, ns.sees, ns.base),
                         ensure_ascii=False))
    else:
        print(json.dumps(comfy_node_info(ns.node, ns.base),
                         ensure_ascii=False, indent=1))

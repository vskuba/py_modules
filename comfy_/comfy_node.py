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
        {'узел': str, 'обязательные': {имя: вход}, 'необязательные': {имя: вход},
        'выходы': [типы]}, где вход — {'тип': str|[], 'значения': [] (если
        enumerate-файл; иначе ''), 'default': что есть}. Для enum-входов
        'значения' — тот самый список: видно, что файл виден загрузчику без
        прогона. `ValueError` словами — если такого узла на ферме нет: проверять
        граф чужой фермой молча означало бы соврать.
    """
    base = base or COMFY_NODE_BASE
    url = f'{base}/object_info/{urllib.request.quote(name)}'
    with urllib.request.urlopen(url, timeout=15) as r:
        body = json.load(r)
    нода = body.get(name)
    if нода is None:
        raise ValueError(f'узел «{name}» на ферме {base} не объявлен; '
                         'спроси имя из /object_info целиком')
    входы = {'required': {}, 'optional': {}}
    for метка in ('required', 'optional'):
        for имя, v in (нода.get('input', {}).get(метка, {}) or {}).items():
            тип = v[0] if isinstance(v, list) and v else v
            extra = v[1] if isinstance(v, list) and len(v) > 1 and v[1] else {}
            entry = ({'type': тип, 'values': []} if isinstance(тип, str)
                     else {'type': 'COMBO', 'values': тип})
            if 'default' in extra:
                entry['default'] = extra['default']
            входы[метка][имя] = entry
    return {'node': name, 'outputs': нода.get('output', []), **входы}


def comfy_node_sees(name: str, файл: str, base: str = '') -> dict:
    """Видит ли загрузчик этот файл (имя в enumerate его входов) — без прогона.

    Возвращает {'виден': bool, 'вход': имя, 'значения': сколько} словами о том,
    где именно имя видно или почему нет.
    """
    info = comfy_node_info(name, base)
    for метка in ('required', 'optional'):
        for имя, вход in info[метка].items():
            if файл in вход.get('values', []):
                return {'visible': True, 'input': имя,
                        'values': len(вход['values'])}
    return {'visible': False, 'input': '', 'values': 0}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='контракт ноды с фермы: входы с типами и файлами, выходы.')
    ap.add_argument('узел', help='класс ноды (`SUPIRApply`, …)')
    ap.add_argument('--base', default='', help='адрес ComfyUI (пусто — локальная)')
    ap.add_argument('--виден', default='', help='файл: видит ли его загрузчик')
    ns = ap.parse_args()
    if ns.виден:
        print(json.dumps(comfy_node_sees(ns.узел, ns.виден, ns.base),
                         ensure_ascii=False))
    else:
        print(json.dumps(comfy_node_info(ns.узел, ns.base),
                         ensure_ascii=False, indent=1))

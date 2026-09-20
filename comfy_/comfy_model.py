"""Инвентарь и докатка весов: что лежит под шаблоном, целое ли, доедет ли с HF.

У плагина-загрузчика свой шаблон имени: граф зовёт `ViT.H.14*s32B*b79K`, а на
диске файл лежит под именем репозитория-источника — метод запускается и молча
рисует не то, пока кто-то не догадается сверить список и не дотянет симлинк-
алиас. А огрызки в 0 байт уже вешали трейнер: «файл есть» не значит «файл
целый». Здесь — оба вопроса одной парой функций: посмотреть (шаблон → файлы,
размер, целость, куда смотрит симлинк, недостающий алиас дотягивается) и
докать (файл с HF сверяется ДО того, как попадёт в дело; битый огрызок не
остаётся в папке).

Знания о конкретном проекте тут нет: папка, шаблон и URL приходят вызывающему.
На ферме те же функции работают поверх `ssh_x` — скрипт-инвентарь доезжает
как есть.
"""
import glob as glob_
import os
import urllib.request


def comfy_model_present(models_dir: str, pattern: str, *,
                        alias: str = '') -> dict:
    """Что из весов под шаблоном лежит и цело ли; недостающий алиас-симлинок дотягивает.

    Args:
        models_dir: папка весов (`/opt/ComfyUI/models/clip_vision`, …).
        pattern: шаблон имени (`*ViT.H.14*s32B*`); ищет glob-ом по имени файла.
        alias: имя симлинка, который должен смотреть на первый найденный
            цельный файл; создаётся, если его нет. Пусто — ничего не создаётся.

    Returns:
        {'найдено': [{'файл', 'байт', 'цел', 'почему', 'куда'}], 'всего': int,
        'алиас': {'имя', 'на', 'статус'} }: 'куда' — на что смотрит симлинк
        (пусто у обычного файла), 'статус' — 'был', 'создан' или 'нет цельного
        файла'. Целость сверяет `file_check`: пустышка, огрызок, голова не по
        расширению.
    """
    from file_.file_check import file_check
    found = []
    for path in sorted(glob_.glob(os.path.join(models_dir, pattern))):
        chk = file_check(path)
        found.append({'file': os.path.basename(path), 'bytes': chk['bytes'],
                      'intact': chk['intact'], 'why': chk['why'],
                      'dest': (os.readlink(path)
                               if os.path.islink(path) else '')})
    алиас = {}
    if alias:
        path = os.path.join(models_dir, alias)
        whole = next((f for f in found if f['intact']), None)
        if os.path.islink(path):
            алиас = {'name': alias, 'target': os.readlink(path), 'status': 'был'}
        elif whole:
            tmp = path + '.tmp'
            try:
                os.symlink(whole['file'], tmp)
                os.replace(tmp, path)
                алиас = {'name': alias, 'target': whole['file'], 'status': 'создан'}
            except OSError as e:
                алиас = {'name': alias, 'target': whole['file'],
                         'status': f'не создался: {e}'}
        else:
            алиас = {'name': alias, 'target': '', 'status': 'нет цельного файла'}
    return {'found': found, 'total': len(found), 'alias': алиас}


def comfy_model_get(folder: str, url: str, *, name: str = '', size: int = 0,
                    sha256: str = '') -> dict:
    """Скачать вес в папку и проверить целость ДО того, как он попадёт в дело.

    Качается во временный файл, сверяется `file_check` (заголовок safetensors,
    размер, хеш — что задано), только целый переименовывается; огрызок удаляется
    и уезжает в ответе словом, а не остаётся в папке притворяться моделью.

    Args:
        folder: папка весов на этой машине.
        url: ссылка на файл (resolve/main репозитория — именованный файл,
            не карточка).
        name: имя файла; пусто — последнее колено URL.
        size: ожидаемый размер, если известен (0 — не сверять).
        sha256: ожидаемый хеш (пусто — не сверять).

    Returns:
        {'цел': bool, 'файл': путь, 'байт': int, 'почему': str} — при провале
        файла на месте нет.
    """
    from file_.file_check import file_check
    name = name or url.rsplit('/', 1)[-1]
    path = os.path.join(folder, name)
    tmp = path + '.part'
    os.makedirs(folder, exist_ok=True)
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return {'intact': False, 'file': '', 'bytes': 0,
                'why': f'не скачался: {e}'}
    chk = file_check(tmp, size, sha256)
    if chk['intact']:
        os.replace(tmp, path)
    return {'intact': chk['intact'], 'file': path if chk['intact'] else '',
            'bytes': chk['bytes'], 'why': chk['why']}


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='инвентарь и докатка весов: что лежит под шаблоном и цело '
                    'ли; или скачать в папку и проверить до постановки в дело.')
    ap.add_argument('mode', choices=['есть', 'докать'],
                    help='«есть» — список под шаблоном, «докать» — скачать')
    ap.add_argument('dir', help='папка весов')
    ap.add_argument('pattern', help='glob-шаблон (есть) или имя файла (докать)')
    ap.add_argument('--alias', default='', help='имя симлинка-алиаса для «есть»')
    ap.add_argument('--url', default='', help='откуда качать для «докать»')
    ap.add_argument('--size', type=int, default=0, help='ожидаемый размер')
    ap.add_argument('--sha', default='', help='ожидаемый sha256')
    ns = ap.parse_args()
    if ns.mode == 'есть':
        print(json.dumps(comfy_model_present(ns.dir, ns.pattern,
                                            alias=ns.alias),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(comfy_model_get(ns.dir, ns.url, name=ns.pattern,
                                         size=ns.size, sha256=ns.sha),
                         ensure_ascii=False))

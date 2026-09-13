"""Состояние коллекции: из журнала манифеста — вердикт, что закрыто и что добить.

comfy_gen пишет манифест журналом строк (по строке на кадр), а вопрос о коллекции
— вопрос состояния: сколько сцен уже закрыто, какие заскорены и каким скором, каких
кадров нет вовсе. Вердикт строится по двум файлам персоны: scenes.dataset.json
(что должно) и collection/manifest.json (что уже нагенерено).
"""
import json
from pathlib import Path


def comfy_collection_status(persona_dir):
    """Вердикт строкой о коллекции персоны: закрыто, слабо, нет кадров.

    persona_dir — каталог персоны: scenes.dataset.json лежит рядом, строки в
    collection/manifest.json кладёт comfy_gen. Сцену с лицом считает закрытой
    только при pass-строке; проваленные показывает с лучшим скором — их добивать
    seed+1000; безлицую считает закрытой любой строкой (скора там не бывает).
    """
    d = Path(persona_dir)
    man = json.loads((d / 'collection' / 'manifest.json').read_text(encoding='utf-8'))
    scenes = json.loads((d / 'scenes.dataset.json').read_text(encoding='utf-8'))
    by_scene = {}
    for r in man:
        by_scene.setdefault(r['scene'], []).append(r)
    done, weak, gone = [], [], []
    for s in scenes:
        rows = by_scene.get(s['id'], [])
        scored = [r['score'] for r in rows if r.get('score') is not None]
        if not rows:
            gone.append(s['id'])
        elif not scored or any(r.get('pass') for r in rows):
            done.append(f"{s['id']} {max(scored):.2f}" if scored else s['id'])
        else:
            weak.append(f"{s['id']} (лучший {max(scored):.2f})")
    return (f"коллекция закрыла {len(done)}/{len(scenes)}"
            + (f"; добить seed+1000: {', '.join(weak)}" if weak else '')
            + (f"; кадров нет вовсе: {', '.join(gone)}" if gone else ''))


if __name__ == '__main__':
    import argparse
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument('persona_dir', help='каталог персоны: scenes.dataset.json + collection/')
    print(comfy_collection_status(**vars(a.parse_args())))

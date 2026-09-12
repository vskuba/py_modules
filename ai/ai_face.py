"""
Лицо кадра: эмбединг и сверка с якорем — «кадр про эту же девушку».

Пачка персоны годится, только если все кадры про одно лицо. Глазом пятьдесят
кадров не сверить, поэтому метрика: косинус эмбединга лица кадра против
эмбединга якорного лица. Модель та же, что на ферме (antelopev2), — проверка
и генерация смотрят одними глазами; качается в root (~/.insightface) сама.

На кадре может быть несколько лиц (витрина, толпа): берётся то, что ближе
всего к якорю, — кадр про неё, даже если она не одна в кадре.

Лицо не нашлось вообще — 0.0: кадр без лица порог не проходит молча, сцены
с face_on=false этого и требуют.
"""
import argparse
import json
from pathlib import Path

# Та же модель лица, что стоит на ферме, — сверка и генерация смотрят
# одними глазами. Имя переопределяется вызовом, как везде в `ai/`.
AI_FACE_MODEL = 'antelopev2'

_apps = {}       # name -> (cv2, FaceAnalysis): тяжёлый стек грузится лениво, один раз
_anchor_cache = {}


def ai_face_score(path, anchor_path, name=AI_FACE_MODEL):
    """Косинус лица кадра против якоря; 0.0, если лицо не нашлось где-либо.

    На кадре несколько лиц — считается максимум по нормированным эмбедингам.
    """
    cv2, app = _face_app(name)
    faces = app.get(cv2.imread(str(path)))
    if not faces:
        return 0.0
    anchor = _anchor_embedding(anchor_path, cv2, app)
    if anchor is None:
        return 0.0
    return round(max(float(f.normed_embedding @ anchor) for f in faces), 3)


def _face_app(name):
    """Один FaceAnalysis на имя: модель тяжёлая, грузится раз."""
    if name not in _apps:
        import cv2  # vision-половина: тяжёлый стек только по требованию
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name=name, root=str(Path.home() / '.insightface'))
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _apps[name] = (cv2, app)
    return _apps[name]


def _anchor_embedding(anchor_path, cv2, app):
    """Эмбединг якоря считается один раз на путь — пачка-то одна и та же."""
    key = str(anchor_path)
    if key not in _anchor_cache:
        faces = app.get(cv2.imread(key))
        _anchor_cache[key] = faces[0].normed_embedding if faces else None
    return _anchor_cache[key]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Сверка кадра (или пачки) с якорным лицом.',
        epilog='check правит score/pass построчно в <dir>/manifest.json; '
               'кадры без лица порог не проходят молча')
    parser.add_argument('command', choices=['score', 'check'])
    parser.add_argument('path', help='файл (score) или каталог с manifest.json (check)')
    parser.add_argument('--anchor', required=True, help='якорное лицо, тот же формат')
    parser.add_argument('--min', type=float, default=0.5,
                        help='порог косинуса для pass (check)')
    ns = parser.parse_args()
    try:
        if ns.command == 'score':
            print(ai_face_score(ns.path, ns.anchor))
        else:
            d = Path(ns.path)
            man = d / 'manifest.json'
            rows = json.loads(man.read_text())
            for r in rows:
                r['score'] = ai_face_score(d / r['file'], ns.anchor)
                r['pass'] = r['score'] >= ns.min
                print(f"{r['file']} {r['scene']} {r['score']} {'ok' if r['pass'] else 'БРАК'}")
            man.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + '\n')
    except (ValueError, FileNotFoundError) as err:
        raise SystemExit(f'ошибка: {err}')

"""
Точки лица как данные: пять опорных, коробка, эллипс — одним вызовом.

Пять точек детектора (глаза, нос, рот) в этих вечерах трижды выковыривались
из insightface одноразовыми скриптами и переносились числами в heredoc'и.
Модуль спрашивает лицо у кадра один раз и отдаёт данные: `kps`/`bbox` для
замеров и маски, эллипс по ним же — без копирования внутренней геометрии
ai_face_paste в каждый скрипт.

Модель та же, что на ферме (antelopev2 через ai_face._face_app): точки
замеров и точки генерации — одни и те же глаза.
"""
import argparse
import json

AI_LANDMARK_DET = (320, 320)   # детальная детекция: на 640 крупные лица дробятся


def ai_landmark(path, anchor='', det_size=AI_LANDMARK_DET):
    """{'kps','bbox'} самого лица кадра; с `anchor` — ближайшего к якорю.

    Ключи — те же имена, что читают ai_face_paste/ai_look_fit: kps — пять
    точек [[x,y]×5] в абсолютных координатах кадра, bbox — коробка лица.
    """
    from ai.ai_face import _face_app
    import cv2
    cv2, app = _face_app('antelopev2', tuple(det_size))
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f'не прочитан кадр: {path}')
    faces = app.get(img)
    if not faces:
        raise ValueError(f'лицо не найдено: {path}')
    if anchor:
        from ai.ai_face import _anchor_embedding
        emb = _anchor_embedding(anchor, cv2, app)
        f = (max(faces, key=lambda x: float(x.normed_embedding @ emb))
             if emb is not None else None) or max(
            faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    else:
        f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    return {'kps': [[round(float(a), 1), round(float(b), 1)] for a, b in f.kps],
            'bbox': [int(v) for v in f.bbox]}


def ai_landmark_ellipse(bbox, kps, mode='face'):
    """Коробка эллипса по kps: тот же овал, что режет mask в ai_face_paste.

    Геометрия скрыта внутри ai_face_paste; здесь она тем же числом наружу —
    листы и замеры кроют ровно ту же область, что потом вшивается. `mode`:
    'face' — овал по точкам (скулы-подбородок-лоб), 'face_hair'/'head_neck'
    — расширить по AI_FACE_MODES (волосы/шея внутри).
    """
    from ai.ai_face import AI_FACE_MODES
    k = [(float(a), float(b)) for a, b in kps]
    x0, y0, x1, y1 = [int(v) for v in bbox]
    if mode == 'face':
        ex0 = min(k[0][0], k[2][0], k[3][0]) - abs(k[1][0] - k[0][0]) * 0.4
        ex1 = max(k[1][0], k[2][0]) + abs(k[1][0] - k[0][0]) * 0.4
        ey = min(k[0][1], k[1][1]) - (k[4][1] - k[0][1]) * 0.35
        eyb = k[4][1] + (k[4][1] - (k[0][1] + k[1][1]) / 2) * 0.15
        return [int(max(0, ex0)), int(max(0, ey)), int(ex1), int(eyb)]
    fw, fh = x1 - x0, y1 - y0
    dt, db, ds = AI_FACE_MODES[mode]
    return [x0 - int(fw * ds), y0 - int(fh * dt), x1 + int(fw * ds),
            y1 + int(fh * db)]


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='ai_landmark',
                                 description='точки/коробка/эллипс лица кадра')
    ap.add_argument('path')
    ap.add_argument('--anchor', default='')
    ap.add_argument('--det', default='320,320')
    ns = ap.parse_args()
    dx, dy = (int(v) for v in ns.det.split(','))
    print(json.dumps(ai_landmark(ns.path, anchor=ns.anchor,
                                det_size=(dx, dy)), ensure_ascii=False))

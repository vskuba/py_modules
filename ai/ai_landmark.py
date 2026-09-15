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
# Лоб выше плотных точек: верхняя граница 106-точечной сетки идёт по бровям,
# волосяной покров модель не размечает. Доля высоты лица (брови→подбородок),
# на которую овал доращивается вверх.
AI_LANDMARK_FOREHEAD = 0.45


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


def ai_landmark_dense(path, anchor='', det_size=AI_LANDMARK_DET):
    """{'kps','bbox','points','contour','pose'} — 106 точек лица и его контур.

    Пять точек говорят только, где глаза и рот: из них овал приходится
    угадывать долями, и он режет лицо по скулам либо тащит фон (замер раунда:
    эллипс по kps 0.305 против прямоугольника 0.689 — оба неверны, просто
    по-разному). У antelopev2 в паке лежит 2d106det, и лицо умеет обвести себя
    само: `points` — 106 точек, `contour` — выпуклая оболочка по ним,
    доращённая вверх на лоб (сетка кончается на бровях), `pose` — углы головы
    (yaw/pitch/roll) от самой модели, без счёта по kps.

    Контур — многоугольник абсолютных координат кадра: он же идёт в маску
    вшивки (`ai_face_mask`), поэтому замер и склейка кроют одну область.
    """
    import numpy as np
    from ai.ai_face import _face_app, _anchor_embedding
    cv2, app = _face_app('antelopev2', tuple(det_size))
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f'не прочитан кадр: {path}')
    faces = app.get(img)
    if not faces:
        raise ValueError(f'лицо не найдено: {path}')
    emb = _anchor_embedding(anchor, cv2, app) if anchor else None
    f = (max(faces, key=lambda x: float(x.normed_embedding @ emb)) if emb is not None
         else max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1])))
    pts = np.asarray(f.get('landmark_2d_106'), 'float32')
    if pts is None or not len(pts):
        raise ValueError(f'в паке нет 2d106det — плотных точек не будет: {path}')
    return {'kps': [[round(float(a), 1), round(float(b), 1)] for a, b in f.kps],
            'bbox': [int(v) for v in f.bbox],
            'points': [[round(float(a), 1), round(float(b), 1)] for a, b in pts],
            'contour': _landmark_contour(pts),
            'pose': [round(float(v), 1) for v in f.get('pose', [0, 0, 0])]}


def ai_landmark_pose(kps, bbox):
    """Ракурс по пяти точкам: (наклон°, поворот, подъём).

    Наклон — угол линии глаз; поворот — насколько нос ушёл от середины глаз (в
    профиль линия сжимается, нос ползёт к краю); подъём — где линия глаз стоит в
    коробке лица. Те же три числа, по которым `ai_look_anchor` подбирает патч
    под ракурс кадра, и по которым оценка датасета считает покрытие углов.
    """
    import math
    (lx, ly), (rx, ry) = kps[0][:2], kps[1][:2]
    roll = math.degrees(math.atan2(float(ry - ly), float(rx - lx)))
    yaw = float(kps[2][0] - (lx + rx) / 2) / max(1.0, float(abs(rx - lx)))
    pitch = (float(ly + ry) / 2 - float(bbox[1])) / max(1.0, float(bbox[3] - bbox[1]))
    return round(roll, 1), round(yaw, 3), round(pitch, 3)


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


def _landmark_contour(pts):
    """Выпуклая оболочка 106 точек, доращённая вверх на лоб; список [x,y].

    Семантика индексов у 2d106 не обещана и меняется между паками, поэтому
    контур берётся оболочкой по ВСЕМ точкам: внешние точки сетки и есть овал
    лица, какой бы ни был порядок. Сетка кончается на бровях — верхние точки
    оболочки поднимаются на AI_LANDMARK_FOREHEAD высоты лица, тем сильнее, чем
    выше точка стоит (подбородок остаётся на месте): получается овал со лбом,
    а не коробка с волосами.
    """
    import cv2
    import numpy as np
    hull = cv2.convexHull(np.asarray(pts, 'float32')).reshape(-1, 2)
    top, bottom = float(hull[:, 1].min()), float(hull[:, 1].max())
    mid = (top + bottom) / 2
    up = (bottom - top) * AI_LANDMARK_FOREHEAD
    out = []
    for x, y in hull:
        # доля подъёма: 0 на середине лица и ниже, 1 у самой верхней точки
        k = max(0.0, (mid - float(y)) / max(1.0, mid - top))
        out.append([round(float(x), 1), round(float(y) - up * k, 1)])
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='ai_landmark',
                                 description='точки/коробка/эллипс лица кадра')
    ap.add_argument('path')
    ap.add_argument('--anchor', default='')
    ap.add_argument('--det', default='320,320')
    ap.add_argument('--dense', action='store_true',
                    help='106 точек, контур лица и углы головы')
    ns = ap.parse_args()
    dx, dy = (int(v) for v in ns.det.split(','))
    fn = ai_landmark_dense if ns.dense else ai_landmark
    print(json.dumps(fn(ns.path, anchor=ns.anchor, det_size=(dx, dy)),
                     ensure_ascii=False))

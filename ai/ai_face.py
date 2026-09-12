"""
Лицо кадра: эмбединг и сверка, выкройка заплатки и обратная склейка.

Пачка персоны годится, только если все кадры про одно лицо. Глазом пятьдесят
кадров не сверить, поэтому метрика: косинус эмбединга лица кадра против
эмбединга якорного лица. Модель та же, что на ферме (antelopev2), — проверка
и генерация смотрят одними глазами; качается в root (~/.insightface) сама.

На кадре может быть несколько лиц (витрина, толпа): берётся то, что ближе
всего к якорю, — кадр про неё, даже если она не одна в кадре.

Лицо не нашлось вообще — 0.0: кадр без лица порог не проходит молча, сцены
с face_on=false этого и требуют.

Второе окно — чужое готовое фото и НАШЕ лицо: `ai_face_crop` выкраивает из
кадра заплатку вокруг лица (режим решает, что входит: только лицо, с волосами
или головой с шеей), ферма генерит только заплатку (апетит в разы меньше целого
кадра), `ai_face_paste` вшивает готовую заплатку обратно со скруглённым пером:
за границами выкройки кадр остаётся байт-в-байт оригиналом.
"""
import argparse
import json
from pathlib import Path

# Та же модель лица, что стоит на ферме, — сверка и генерация смотрят
# одними глазами. Имя переопределяется вызовом, как везде в `ai/`.
AI_FACE_MODEL = 'antelopev2'

# Режимы выкройки: насколько расширить коробку лица со сторон (доля коробки):
# (сверху — вверх на волосы, снизу — вниз на шею, с боков).
AI_FACE_MODES = {
    'face': (0.35, 0.15, 0.25),   # только лицо: подбородок-брови + чуток
    'face_hair': (0.9, 0.25, 0.5),  # причёска тоже станет нашей
    'head_neck': (0.5, 0.9, 0.45),  # голова целиком + шея
}
AI_FACE_FEATHER = 0.08   # радиус пера склейки как доля меньшей стороны выкройки

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


def ai_face_crop(path, out, mode='face'):
    """Выкроить лицо из кадра в файл-заплатку; вернуть {'box','mode','out'}.

    Лиц несколько — берётся самое крупное. Коробка лица расширяется по режиму
    `mode` (ключ AI_FACE_MODES) и обрезается по краям кадра; заплатка — ровно
    эти пиксели, без ресайза: генератор платит только за них.
    """
    cv2, app = _face_app(AI_FACE_MODEL)
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f'не прочитан кадр: {path}')
    faces = app.get(img)
    if not faces:
        raise ValueError(f'лицо не найдено: {path}')
    f = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    x0, y0, x1, y1 = (int(v) for v in f.bbox)
    dt, db, ds = AI_FACE_MODES[mode]
    fw, fh = x1 - x0, y1 - y0
    h, w = img.shape[:2]
    box = (max(0, x0 - int(fw * ds)), max(0, y0 - int(fh * dt)),
           min(w, x1 + int(fw * ds)), min(h, y1 + int(fh * db)))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img[box[1]:box[3], box[0]:box[2]])
    return {'box': list(box), 'mode': mode, 'out': str(out)}


def ai_face_paste(base, patch, out, box, feather=AI_FACE_FEATHER):
    """Вшить заплатку в кадр скруглённым пером; вернуть {'out','box'}.

    Вне `box` ([x0,y0,x1,y1]) кадр не меняется вовсе; внутри — заплатка (при
    нужде ресайвится Ланцошем под размер выкройки), край пера размывается на
    feather*минус-сторона. Так чужое фото становится нашим, не теряя фон и
    крой оригинала.
    """
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    base_img = cv2.imread(str(base))
    if base_img is None:
        raise ValueError(f'не прочитан кадр: {base}')
    x0, y0, x1, y1 = (int(v) for v in box)
    bw, bh = x1 - x0, y1 - y0
    im = Image.open(patch)
    if im.size != (bw, bh):
        im = im.resize((bw, bh), Image.LANCZOS)
    ph = np.asarray(im.convert('RGB'))[:, :, ::-1]           # RGB -> BGR под cv2
    m = Image.new('L', (bw, bh), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, bw - 1, bh - 1],
                                       radius=min(bw, bh) // 4, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(max(1, int(min(bw, bh) * feather))))
    pm = np.asarray(m)[..., None].astype(np.float32) / 255.0
    roi = base_img[y0:y1, x0:x1].astype(np.float32)
    base_img[y0:y1, x0:x1] = roi * (1 - pm) + ph * pm
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), base_img)
    return {'out': str(out), 'box': [x0, y0, x1, y1]}


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
        description='Сверка кадра с якорем; выкроить заплатку лица / вшить готовую.')
    parser.add_argument('command', choices=['score', 'check', 'crop', 'paste'])
    parser.add_argument('path', help='файл (score/check/crop/paste-основа)')
    parser.add_argument('patch', nargs='?', default='', help='paste: файл-заплатка')
    parser.add_argument('--anchor', default='', help='якорное лицо для score/check')
    parser.add_argument('--min', type=float, default=0.5,
                        help='порог косинуса для pass (check)')
    parser.add_argument('--out', default='', help='куда: score-файл, чек не трогает')
    parser.add_argument('--mode', default='face', choices=list(AI_FACE_MODES),
                        help='crop: что входит в выкройку')
    parser.add_argument('--box', default='', help='paste: «x0,y0,x1,y1» из crop')
    ns = parser.parse_args()
    try:
        if ns.command == 'score':
            print(ai_face_score(ns.path, ns.anchor))
        elif ns.command == 'check':
            d = Path(ns.path)
            man = d / 'manifest.json'
            rows = json.loads(man.read_text())
            for r in rows:
                r['score'] = ai_face_score(d / r['file'], ns.anchor)
                r['pass'] = r['score'] >= ns.min
                print(f"{r['file']} {r['scene']} {r['score']} {'ok' if r['pass'] else 'БРАК'}")
            man.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + '\n')
        elif ns.command == 'crop':
            if not ns.out:
                raise SystemExit('crop требует --out (файл-заплатка)')
            r = ai_face_crop(ns.path, ns.out, mode=ns.mode)
            print(json.dumps(r, ensure_ascii=False))
        else:
            if not (ns.patch and ns.out and ns.box):
                raise SystemExit('paste требуют заплатку, --out и --box из crop')
            r = ai_face_paste(ns.path, ns.patch, ns.out,
                              [int(v) for v in ns.box.split(',')])
            print(json.dumps(r, ensure_ascii=False))
    except (ValueError, KeyError, FileNotFoundError) as err:
        raise SystemExit(f'ошибка: {err}')

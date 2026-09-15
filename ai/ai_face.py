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
AI_FACE_DET = (640, 640)  # размер детекции по умолчанию; крайний ракурс/мелкое
                          # лицо ловится на меньшем det_size — зовёт другой вызов

_apps = {}       # name -> (cv2, FaceAnalysis): тяжёлый стек грузится лениво, один раз
_anchor_cache = {}


def ai_face_score(path, anchor_path, name=AI_FACE_MODEL, det_size=AI_FACE_DET):
    """Косинус лица кадра против якоря; 0.0, если лицо не нашлось где-либо.

    На кадре несколько лиц — считается максимум по нормированным эмбедингам.
    Крайний ракурс/мелкое лицо — зови с тем же `det_size`, что кроил.
    `anchor_path` — файл якоря либо готовый эмбединг (список чисел из
    `ai_face_centroid`): центроид пачки судит вернее одного кадра.
    """
    import numpy as np
    cv2, app = _face_app(name, det_size, modules=('detection', 'recognition'))
    faces = app.get(cv2.imread(str(path)))
    if not faces:
        return 0.0
    if isinstance(anchor_path, (list, tuple)):
        anchor = np.asarray(anchor_path, 'float32')
    else:
        anchor = _anchor_embedding(anchor_path, cv2, app)
    if anchor is None:
        return 0.0
    return round(max(float(f.normed_embedding @ anchor) for f in faces), 3)


def ai_face_centroid(paths, name=AI_FACE_MODEL, det_size=AI_FACE_DET):
    """Центроид эмбедингов пачки; {'embedding','n','coherence'}.

    Один якорь — плохой судья: его косинус несёт не только «та ли это женщина»,
    но и ракурс, свет и качество самого якоря. Центроид пачки от ракурса
    свободен, и рядом с ним есть потолок — `coherence`, медианный косинус самих
    патчей к нему: выше него не прыгнет ни один результат, потому что столько
    же дают НАСТОЯЩИЕ фотографии этого человека (замер diana: 0.78 по 79
    патчам, а чужое лицо кадра даёт −0.14 — вот и вся шкала).

    Считать каждый раз заново дорого (лицо детектится на каждом файле), потому
    зовущий обычно считает центроид один раз на персону и носит с собой.

    `faces` — построчно по файлам: коробка, пять точек, уверенность детектора,
    сам эмбединг и косинус К ЦЕНТРОИДУ. Он считается тем же проходом: оценке датасета нужно и
    то, и другое, а вторая детекция сотни файлов стоит минуты на пустом месте.
    """
    import numpy as np
    # только детекция и эмбединг: точки, поза и пол-возраст центроиду не нужны,
    # а на пачке в две сотни патчей их прогон и составляет почти всё время
    cv2, app = _face_app(name, det_size, modules=('detection', 'recognition'))
    embs, rows = [], []
    for p in map(Path, paths):
        img = cv2.imread(str(p))
        if img is None:
            continue
        faces = app.get(img)
        if not faces:
            continue
        f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
        embs.append(f.normed_embedding)
        rows.append({'file': p.name, 'bbox': [int(v) for v in f.bbox],
                     'emb': [round(float(v), 5) for v in f.normed_embedding],
                     'kps': [[round(float(a), 1), round(float(b), 1)] for a, b in f.kps],
                     'det': round(float(f.det_score), 3), 'faces': len(faces),
                     'size': [int(img.shape[1]), int(img.shape[0])]})
    if not embs:
        raise ValueError('ни на одном файле пачки не нашлось лица')
    c = np.mean(embs, axis=0)
    c = c / np.linalg.norm(c)
    for row, e in zip(rows, embs):
        row['cos'] = round(float(e @ c), 3)
    return {'embedding': [round(float(v), 6) for v in c], 'n': len(embs),
            'coherence': round(float(np.median([e @ c for e in embs])), 3),
            'faces': rows}


def ai_face_crop(path, out, mode='face', det_size=AI_FACE_DET, anchor=''):
    """Выкроить лицо из кадра в файл-заплатку; вернуть {'box','mode','out'}.

    Лиц несколько — с якорем берётся ближайшее к нему (кадр про неё, даже
    если она не одна в кадре: чужое лицо рядом не должно уезжать в заплатку),
    без якоря — самое крупное. Коробка лица расширяется по режиму `mode`
    (ключ AI_FACE_MODES) и обрезается по краям кадра; заплатка — ровно
    эти пиксели, без ресайза: генератор платит только за них.

    Крайний ракурс или мелкое лицо ловятся не на любом det_size: на крупном
    детектор дробит лицо до несерьёзных пикселей и не берёт его — зовущий
    передаёт `det_size` помельче.
    """
    cv2, app = _face_app(AI_FACE_MODEL, det_size,
                        modules=('detection', 'recognition'))
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f'не прочитан кадр: {path}')
    faces = app.get(img)
    if not faces:
        raise ValueError(f'лицо не найдено: {path}')
    emb = _anchor_embedding(anchor, cv2, app) if anchor and len(faces) > 1 else None
    if emb is not None:
        f = max(faces, key=lambda x: float(x.normed_embedding @ emb))
    else:
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
    return {'box': list(box), 'mode': mode, 'out': str(out),
            'kps': [[round(float(a), 1), round(float(b), 1)] for a, b in f.kps]}


def ai_face_paste(base, patch, out, box, feather=AI_FACE_FEATHER, kps=None,
                  hf=0, mask=''):
    """Вшить заплатку в кадр пером по форме лица; вернуть {'out','box'}.

    `mask` — готовая серая маска кадра (`ai_mask_face`: контур по 106 точкам
    минус пряди и пальцы поверх лица). Она уже с пером в пикселях, поэтому
    `feather` к ней не применяется, а форму никто не угадывает долями. Это
    основной путь; ветки ниже — для вызовов, у которых маски нет.

    Без `mask`: эллипс по пяти точкам детектора (скулы, подбородок, лоб), а не
    прямоугольник выкройки: вне эллипса кадр не тронут вовсе (причёска и фон
    остаются оригинальные), край пера размывается на feather*минус-сторона.
    Склейка через лапласианову пирамиду (3 уровня): цветовая растяжка идёт по
    всему перу, а не по одному краю, — скачок шва меряет ai_look_integrity.
    `hf` > 0 — частотная склейка: `hf` самых мелких уровней пирамиды берут
    текстуру кадра (поры, ресницы, зерно — байт в байт оригинал), от заплатки
    остаётся только низкочастотная геометрия; hf=0 — заплатка на всех уровнях,
    как было. Для чужого кадра hf по замерам вредит (замер 2026-09-14: 0.537
    против 0.363 при hf=1): мелкая текстура чужого лица — тоже чужая. Без
    `kps` (абсолютные координаты глаз/рта выкройки) — скруглённый прямоугольник
    по боксу.
    """
    import cv2
    import numpy as np
    base_img = cv2.imread(str(base))
    if base_img is None:
        raise ValueError(f'не прочитан кадр: {base}')
    x0, y0, x1, y1 = (int(v) for v in box)
    bw, bh = x1 - x0, y1 - y0
    ph = cv2.resize(cv2.imread(str(patch)), (bw, bh),
                    interpolation=cv2.INTER_LANCZOS4).astype(np.float32)
    roi = base_img[y0:y1, x0:x1].astype(np.float32)
    m = np.zeros((bh, bw), np.float32)
    if mask:
        mm = cv2.imread(str(mask), cv2.IMREAD_GRAYSCALE)
        if mm is None:
            raise ValueError(f'не прочитана маска: {mask}')
        # маска кадра — режем по боксу; маска размером с заплатку — как есть
        if mm.shape[:2] == base_img.shape[:2]:
            mm = mm[y0:y1, x0:x1]
        m = cv2.resize(mm, (bw, bh)).astype(np.float32) / 255
    elif kps is not None:
        k = np.asarray(kps, np.float32) - np.float32([x0, y0])
        ey, my = (k[0][1] + k[1][1]) / 2, (k[3][1] + k[4][1]) / 2   # глаза, рот
        top, bot = ey - (my - ey) * 0.8, my + (my - ey) * 1.15      # лоб, подбородок
        cv2.ellipse(m, (int((k[0][0] + k[1][0]) / 2), int((top + bot) / 2)),
                    (int(abs(k[1][0] - k[0][0]) * 1.15), int((bot - top) / 2)),
                    0, 0, 360, 1.0, -1)
    else:
        r = min(bw, bh) // 4
        cv2.rectangle(m, (r, 0), (bw - 1 - r, bh - 1), 1.0, -1)
        cv2.rectangle(m, (0, r), (bw - 1, bh - 1 - r), 1.0, -1)
        for cx, cy in ((r, r), (bw - 1 - r, r),
                       (r, bh - 1 - r), (bw - 1 - r, bh - 1 - r)):
            cv2.circle(m, (int(cx), int(cy)), r, 1.0, -1)
    if not mask:   # готовая маска приходит с пером в пикселях — не размываем
        m = cv2.GaussianBlur(m, (0, 0), max(1.0, min(bw, bh) * feather))
    gb, gp, gm, lb = [roi], [ph], [m], []

    def fit(x, shape):
        # нечётная сторона: пир-ап возвращает на пиксель больше — режем по тонкому уровню
        return x[:shape[0], :shape[1]] if x.shape[:2] != tuple(shape) else x

    for _ in range(3):
        b, p = cv2.pyrDown(gb[-1]), cv2.pyrDown(gp[-1])
        lb.append((gb[-1] - fit(cv2.pyrUp(b), gb[-1].shape),
                   gp[-1] - fit(cv2.pyrUp(p), gp[-1].shape)))
        gb.append(b)
        gp.append(p)
        gm.append(cv2.pyrDown(gm[-1]))
    lb.append((gb[-1], gp[-1]))
    r = lb[-1][1] * gm[-1][..., None] + lb[-1][0] * (1 - gm[-1][..., None])
    for lvl in range(len(lb) - 2, -1, -1):
        bb, pp = lb[lvl]
        # hf самых мелких уровней — без маски: их остаток даёт сам кадр (HF);
        # заплатка доходит только до грубых уровней (LF — геометрия и цвет).
        g = np.zeros_like(gm[lvl][..., None]) if lvl < hf else gm[lvl][..., None]
        r = fit(cv2.pyrUp(r), bb.shape) + pp * g + bb * (1 - g)
    # КЛИП обязателен: лапласианова пирамида даёт выброс за границы на резком
    # перепаде (светлая кожа против тёмных волос), а присваивание float32 в
    # uint8-массив numpy заворачивает по модулю — 260 становится 4. На контуре
    # лица это выглядело как чистая синяя кромка: R и G ушли в ноль, B остался
    # (замер istockphoto-653141840).
    base_img[y0:y1, x0:x1] = np.clip(r, 0, 255).astype(base_img.dtype)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), base_img)
    return {'out': str(out), 'box': [x0, y0, x1, y1]}


def _face_app(name, det=AI_FACE_DET, modules=None):
    """FaceAnalysis на (имя, det_size, состав): модель тяжёлая, грузится раз.

    `modules` — какие модели пака поднимать (`allowed_modules` insightface).
    По умолчанию весь пак: детекция, точки 2d106 и 3d68, пол-возраст, эмбединг —
    пять прогонов на кадр. Тому, кому нужен только косинус, четыре из них лишние,
    а на пачке в две сотни патчей это минуты (`ai_face_centroid`).
    """
    key = (name, tuple(det), tuple(modules) if modules else None)
    if key not in _apps:
        import cv2  # vision-половина: тяжёлый стек только по требованию
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name=name, root=str(Path.home() / '.insightface'),
                           allowed_modules=list(modules) if modules else None)
        app.prepare(ctx_id=-1, det_size=tuple(det))
        _apps[key] = (cv2, app)
    return _apps[key]


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
    parser.add_argument('--anchor', default='',
                        help='якорное лицо для score/check/crop (лицо в кадре — ближайшее к нему)')
    parser.add_argument('--min', type=float, default=0.5,
                        help='порог косинуса для pass (check)')
    parser.add_argument('--out', default='', help='куда: score-файл, чек не трогает')
    parser.add_argument('--mode', default='face', choices=list(AI_FACE_MODES),
                        help='crop: что входит в выкройку')
    parser.add_argument('--det', default='',
                        help='детекция «W,H» (например 320,320) для score/check/crop; '
                             'по умолчанию AI_FACE_DET')
    parser.add_argument('--box', default='', help='paste: «x0,y0,x1,y1» из crop')
    parser.add_argument('--hf', type=int, default=0,
                        help='paste: сколько мелких уровней пирамиды берут текстуру '
                             'кадра (частотная склейка LF(заплатка)+HF(кадр); 0 — нет)')
    parser.add_argument('--mask', default='',
                        help='paste: готовая серая маска (ai_mask): контур лица '
                             'минус пряди и пальцы поверх него')
    ns = parser.parse_args()
    det = tuple(int(v) for v in ns.det.split(',')) if ns.det else AI_FACE_DET
    try:
        if ns.command == 'score':
            print(ai_face_score(ns.path, ns.anchor, det_size=det))
        elif ns.command == 'check':
            d = Path(ns.path)
            man = d / 'manifest.json'
            rows = json.loads(man.read_text())
            for r in rows:
                # патч-манифест: меряем заплатку; кадровый — сам кадр
                target = d / r.get('patch', r['file'])
                try:
                    r['score'] = ai_face_score(target, ns.anchor, det_size=det)
                    r['pass'] = r['score'] >= ns.min
                except Exception as e:
                    r['score'] = None
                    print(f'пропущен {target.name}: {e}')
                    continue
                print(f"{target.name} {r.get('scene', '')} {r['score']} "
                      f"{'ok' if r['pass'] else 'БРАК'}".rstrip())
            man.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + '\n')
        elif ns.command == 'crop':
            if not ns.out:
                raise SystemExit('crop требует --out (файл-заплатка или каталог)')
            if Path(ns.path).is_dir():
                # пачка: все кадры каталога в заплатки, строки — в out/manifest.json
                d, od = Path(ns.path), Path(ns.out)
                od.mkdir(parents=True, exist_ok=True)
                mp = od / 'manifest.json'
                man = json.loads(mp.read_text()) if mp.exists() else []
                done = {r['file'] for r in man}
                for src in sorted(p for p in d.iterdir()
                                  if p.suffix.lower() in ('.jpg', '.jpeg', '.png')
                                  and p.stem != 'manifest'):
                    if src.name in done:
                        continue
                    try:
                        r = ai_face_crop(src, od / (src.stem + '.png'), mode=ns.mode,
                                         det_size=det, anchor=ns.anchor)
                    except ValueError as e:
                        print(f'пропущен {src.name}: {e}')
                        continue
                    r['patch'] = Path(r.pop('out')).name
                    r['file'] = src.name
                    man.append(r)
                    print(src.name, r['box'])
                mp.write_text(json.dumps(man, ensure_ascii=False, indent=1) + '\n')
            else:
                r = ai_face_crop(ns.path, ns.out, mode=ns.mode, det_size=det,
                                 anchor=ns.anchor)
                print(json.dumps(r, ensure_ascii=False))
        else:
            if not (ns.patch and ns.out and ns.box):
                raise SystemExit('paste требуют заплатку, --out и --box из crop')
            r = ai_face_paste(ns.path, ns.patch, ns.out,
                              [int(v) for v in ns.box.split(',')], hf=ns.hf,
                              mask=ns.mask)
            print(json.dumps(r, ensure_ascii=False))
    except (ValueError, KeyError, FileNotFoundError) as err:
        raise SystemExit(f'ошибка: {err}')

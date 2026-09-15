"""
Маска вшивки: какая область кадра наша, а какая обязана остаться кадром.

Овал лица — это ещё не ответ на вопрос «куда класть заплатку». Поперёк лица
лежат пряди волос, к подбородку подходят пальцы, на щёку падает лямка: всё
это ближе к зрителю, чем лицо, и заплатка обязана их обойти. Прямоугольник
стирает их начисто — и кадр становится фальшивым раньше, чем зритель успеет
посмотреть на само лицо.

Перекрытия ищутся не семантикой, а формой: прядь — это тонкая тёмная
структура на светлом (blackhat берёт её при любом общем затенении, потому что
меряет пиксель против его же окрестности), волосяная масса — просто сильно
темнее кожи. Оба признака считаются внутри овала, от медианы кожи самого
кадра, поэтому порогов «в абсолютных единицах» здесь нет.

Перо — в ПИКСЕЛЯХ, а не в долях стороны: лицо бывает 100 px и бывает 1000, а
шов глазом виден по абсолютной ширине размытия.
"""
import argparse
import json

# Тёмное считается перекрытием ниже этой доли от медианной яркости кожи овала.
AI_MASK_DARK = 0.62
# Порог тонкой тёмной структуры: сколько сигм blackhat считать прядью.
AI_MASK_STRAND = 2.0
# Кисть blackhat как доля высоты лица: прядь тоньше — берётся, коса толще — нет.
AI_MASK_STRAND_K = 0.09
# Какая доля тёмного куска обязана лежать СНАРУЖИ овала, чтобы счесть его
# перекрытием, а не чертой лица: бровь и губы целиком внутри, прядь — нет.
AI_MASK_OUTSIDE = 0.15
# Перекрытия съели больше этой доли овала — замер не удался (тень принята за
# волосы); маска откатывается к чистому овалу, а вердикт говорит об этом.
AI_MASK_OCCLUDE_MAX = 0.55


def ai_mask_face(photo, contour, out='', feather=3.0, occlusion=True,
                 dark=AI_MASK_DARK, strand=AI_MASK_STRAND):
    """Маска лица кадра минус перекрытия; {'out','box','cover','occluded','fallback'}.

    `contour` — многоугольник из `ai_landmark_dense` (абсолютные координаты).
    Внутри него ищутся перекрытия (`occlusion`): тонкие тёмные структуры
    (пряди) и всё, что темнее `dark`×медианы кожи (волосяная масса, глубокая
    тень под подбородком). `feather` — радиус размытия края в пикселях.

    `cover` — доля кадра под маской, `occluded` — какую долю овала забрали
    перекрытия, `fallback` — правда, если перекрытия забрали больше
    AI_MASK_OCCLUDE_MAX овала и маска откатилась к чистому овалу: на тёмном
    кадре медиана кожи может уехать, и тогда «перекрытием» становится само лицо.
    """
    import cv2
    import numpy as np
    from pathlib import Path
    img = cv2.imread(str(photo))
    if img is None:
        raise ValueError(f'не прочитан кадр: {photo}')
    h, w = img.shape[:2]
    poly = np.asarray([[int(round(x)), int(round(y))] for x, y in contour], np.int32)
    oval = np.zeros((h, w), np.uint8)
    cv2.fillPoly(oval, [poly], 255)
    inside = oval > 0
    if not inside.any():
        raise ValueError('контур пуст: маске нечего закрывать')
    m = oval.copy()
    occluded = 0.0
    fallback = False
    if occlusion:
        occ = ai_mask_occlusion(img, inside, dark=dark, strand=strand)
        occluded = float((occ & inside).sum()) / float(inside.sum())
        if occluded > AI_MASK_OCCLUDE_MAX:
            fallback = True   # замер уехал: овал целиком «перекрыт» — не верим
        else:
            m[occ] = 0
    if feather > 0:
        m = cv2.GaussianBlur(m, (0, 0), float(feather))
    x0, y0 = int(poly[:, 0].min()), int(poly[:, 1].min())
    x1, y1 = int(poly[:, 0].max()), int(poly[:, 1].max())
    box = [max(0, x0), max(0, y0), min(w, x1), min(h, y1)]
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(p), m)
    return {'out': str(out), 'box': box, 'cover': round(float(m.mean()) / 255, 4),
            'occluded': round(occluded, 3), 'fallback': fallback}


def ai_mask_occlusion(img, inside, dark=AI_MASK_DARK, strand=AI_MASK_STRAND):
    """Булева карта перекрытий кадра: пряди и тёмная масса внутри овала.

    `img` — кадр BGR, `inside` — булева карта овала (что считать лицом). Тон
    кожи берётся медианой яркости внутри овала: пряди и тень — меньшинство
    пикселей лица, медиану они не сдвигают, а вот среднее сдвинули бы.

    Тёмное внутри овала — ещё не перекрытие: бровь, ресница, ноздря и линия
    губ тоже тёмные, и вычесть их значит оставить в заплатке чужие черты.
    Различает их не яркость, а СВЯЗНОСТЬ: прядь приходит из причёски и тянется
    за границу овала, а бровь лежит целиком внутри. Поэтому кусок тёмного
    считается перекрытием, только если заметная его часть — снаружи овала
    (AI_MASK_OUTSIDE), и отбрасывается, если он весь внутри.
    """
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ys, _ = np.where(inside)
    face_h = max(8, int(ys.max() - ys.min()))
    skin = float(np.median(gray[inside]))
    k = max(3, int(face_h * AI_MASK_STRAND_K) | 1)   # нечётная сторона кисти
    bh = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    vals = bh[inside].astype('float32')
    thr = float(vals.mean() + strand * vals.std())
    raw = ((bh > max(6.0, thr)) | (gray < skin * dark)).astype('uint8')
    raw = (cv2.medianBlur(raw * 255, 3) > 0).astype('uint8')  # одиночные — шум
    n, lab, stats, _ = cv2.connectedComponentsWithStats(raw, connectivity=8)
    occ = np.zeros_like(raw, bool)
    for i in range(1, n):
        comp = lab == i
        in_n = int((comp & inside).sum())
        if not in_n:
            continue                      # овала не касается — не наше дело
        out_n = int(stats[i, cv2.CC_STAT_AREA]) - in_n
        if out_n >= max(8, AI_MASK_OUTSIDE * stats[i, cv2.CC_STAT_AREA]):
            occ |= comp                   # пришло снаружи: прядь, палец, лямка
    return cv2.dilate(occ.astype('uint8'), np.ones((3, 3), np.uint8)) > 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='ai_mask',
                                 description='маска лица кадра минус перекрытия')
    ap.add_argument('photo')
    ap.add_argument('--out', required=True, help='png маски (серый)')
    ap.add_argument('--overlay', default='', help='кадр с маской поверх — глазами')
    ap.add_argument('--feather', type=float, default=3.0, help='перо, ПИКСЕЛЕЙ')
    ap.add_argument('--no-occlusion', action='store_true',
                    help='чистый овал, без поиска прядей')
    ap.add_argument('--dark', type=float, default=AI_MASK_DARK)
    ap.add_argument('--strand', type=float, default=AI_MASK_STRAND)
    ns = ap.parse_args()
    from ai.ai_landmark import ai_landmark_dense
    lm = ai_landmark_dense(ns.photo)
    r = ai_mask_face(ns.photo, lm['contour'], ns.out, feather=ns.feather,
                     occlusion=not ns.no_occlusion, dark=ns.dark, strand=ns.strand)
    if ns.overlay:
        import cv2
        import numpy as np
        img = cv2.imread(ns.photo)
        m = cv2.imread(ns.out, cv2.IMREAD_GRAYSCALE).astype('float32') / 255
        tint = np.zeros_like(img, 'float32')
        tint[..., 2] = 255      # красным — что заменится
        cv2.imwrite(ns.overlay, (img * (1 - m[..., None] * 0.55) +
                                 tint * (m[..., None] * 0.55)).astype('uint8'))
        r['overlay'] = ns.overlay
    print(json.dumps(r, ensure_ascii=False))

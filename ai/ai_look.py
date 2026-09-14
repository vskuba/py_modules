"""
Замеры кадра для самоуверенной заплатки: кожа, волосы, свет, резкость, лишнее.

Лицо натянули — претензии к кадру принимаются по цифрам, не глазами: кожа не
та, волосы не того цвета, плечо обрезано, лицо мыльное. Чтобы править самим,
у кадра сначала спрашиваем (probe): какой тон кожи у оригинала, какой цвет и
доля волос в выкройке, какой свет, какая резкость. Промпт графа собирается из
замеров (prompt) — модель рисует то, что на кадре есть, а не что ей показалось.
Заплатку после генерации подтягиваем к замерам: цвет (fit), резкость (sharpen).
Целостностьcomposite после вшивки — это integrity: вне выкройки кадр обязан
быть байт-в-байт оригинал, а скачок на шве — по перу, не по плечу.

Тяжёлый стек (cv2/numpy/PIL, vision-модель) — лениво внутри функций.
"""

# Ближайший тон подбирается по этим эталонам — имена уходят в промпт графа.
AI_LOOK_HAIR_COLORS = {
    'black': (24, 22, 24), 'dark brown': (62, 42, 34), 'brown': (112, 76, 54),
    'light brown': (152, 112, 76), 'blond': (206, 172, 124),
    'red': (156, 68, 46), 'auburn': (122, 58, 44), 'grey': (148, 144, 140),
}
AI_LOOK_HAIR_COVER = 0.2   # доля пикселей полосы, похожих на волосы, — говорить про причёску
# Что не должно быть у лица-якоря: пальцы у подбородка лезут в заплатку вместе
# с лицом — якорь берут без них.
AI_LOOK_ANCHOR_HANDS = ('рук', 'пал', 'ногот')
# Масштабы рассогласования ракурса: наклон ° / поворот (смещ. носа) / подъём (глаза в коробке).
AI_LOOK_POSE = (30.0, 0.5, 0.5)
# Гибридный ранг якоря: доля косинуса к лицу кадра / к центроиду датасета
# (один косинус к исходнику выбирает бракованную заплатку на асимметрии кадра).
AI_LOOK_RANK = (0.7, 0.3)
# Коридор множителя канальной аффины: на плоской заплатке std_gen≈0 не рушит шум.
AI_LOOK_FIT_CLIP = (0.8, 1.25)


def ai_look_probe(photo, box):
    """Снять замеры с выкройки оригинала: кожа, волосы, свет, резкость.

    `photo` — файл, `box` — выкройка [x0,y0,x1,y1] из ai_face_crop. Кожа —
    тон ядра (центр выкройки: нос/щёки), но не по всем пикселям, а по
    межквартильному диапазону яркости (25–75%): тень, блик с носа и помада
    в истинный тон подкожного слоя не входят; волосы — тот же диапазон по
    пикселям полосы над бровями, не похожих на кожу (мало таких — hair=None,
    причёску в промпт не тащим), свет — мода яркости выкройки, резкость —
    дисперсия Лапласиана серой выкройки (эталон, до которого потом доводим
    заплатку).
    """
    import cv2
    import numpy as np
    x0, y0, x1, y1 = (int(v) for v in box)
    img = cv2.imread(str(photo))
    if img is None:
        raise ValueError(f'не прочитан кадр: {photo}')
    crop = img[y0:y1, x0:x1]
    h, w = crop.shape[:2]
    core = _look_core(crop[int(h * 0.3):int(h * 0.85),
                            int(w * 0.25):int(w * 0.75)].reshape(-1, 3)
                      .astype('float32'))
    band = crop[0:int(h * 0.35), :].reshape(-1, 3).astype('float32')
    skin = np.median(core, axis=0)
    far = np.linalg.norm(band - skin, axis=1)
    hair_px = _look_core(band[far > 60]) if (far > 60).any() else band[:0]
    cover = float((far > 60).mean())
    hair = ([round(float(v), 1) for v in np.median(hair_px, axis=0)[::-1]]
            if cover >= AI_LOOK_HAIR_COVER and len(hair_px) else None)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return {'skin': [round(float(v), 1) for v in skin[::-1]],
            'skin_std': [round(float(v), 1) for v in core.std(0)[::-1]],
            'hair': hair, 'hair_cover': round(cover, 2),
            'light': float(np.median(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))),
            'sharp': round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1)}


def ai_look_prompt(probe):
    """Собрать промпт из замеров: причёска, свет — какими они на кадре есть.

    Триггер персоны кладёт зовущий сверху; здесь — только дополняющий хвост,
    целиком из цифр probe. Молчит, когда мерить нечего.
    """
    bits = ['face close-up']
    if probe.get('hair'):
        name = _look_color_name(probe['hair'])
        long_ = probe.get('hair_cover', 0) >= 0.5
        bits.append(('long ' if long_ else '') + name + ' hair')
    l = probe.get('light', 128)
    bits.append('soft light' if l < 150 else 'bright light')
    if l < 70:
        bits.append('low light')
    return ', ' + ', '.join(bits)


def ai_look_fit(patch, probe, out):
    """Подтянуть цвет заплатки к замерам оригинала; {'before','after','moved'}.

    Комящая заплатка помнит цвет датасета: кожа светлее, каналы растянуты не
    так (синий тонет). Канальная аффина New=(Gen−mean_gen)·std_src/std_gen+
    mean_src по тому же межквартильному ядру, что и probe: и сдвиг, и масштаб
    канала; множитель запутан клипом [AI_LOOK_FIT_CLIP] — на плоской заплатке
    std_gen≈0 не должен разгонять шум. Без probe['skin_std'] — сдвиг, без
    масштаба (обратная совместимость).
    """
    import cv2
    import numpy as np
    img = cv2.imread(str(patch))
    if img is None:
        raise ValueError(f'не прочитана заплатка: {patch}')
    h, w = img.shape[:2]
    core = _look_core(img[int(h * 0.3):int(h * 0.85),
                          int(w * 0.25):int(w * 0.75)].reshape(-1, 3)
                      .astype('float32'))
    before = [round(float(v), 1) for v in np.median(core, axis=0)[::-1]]
    mu_g, sd_g = core.mean(0), core.std(0)
    mu_s = np.array(probe['skin'][::-1], 'float32')
    if probe.get('skin_std'):
        sd_s = np.array(probe['skin_std'][::-1], 'float32')
        ratio = np.clip(np.divide(sd_s, sd_g, out=np.ones_like(sd_s),
                                 where=sd_g > 2),
                        AI_LOOK_FIT_CLIP[0], AI_LOOK_FIT_CLIP[1])
    else:
        ratio = np.ones(3, 'float32')
    img = np.clip((img.astype('float32') - mu_g) * ratio + mu_s, 0, 255).astype('uint8')
    from pathlib import Path
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    return {'before': before, 'after': probe['skin'],
            'moved': bool(np.abs(np.round(ratio - 1, 3)).max() > 0.02
                         or max(abs(mu_s - mu_g)) > 1)}


def ai_look_sharpen(patch, out, sharp):
    """Довести резкость заплатки до замеров оригинала; {'sharp','want','applied'}.

    Пиксели генератора мягче пикселей фото: заплатка, разогнанная до 512 и
    сжатая обратно, теряет контраст граней. Дисперсия Лапласиана заплатки ниже
    `sharp` (из probe) — unsharp маской, одной итерацией с фиксированной силой,
    пока не дотянули; резче оригинала не делаем — исходник не точим.
    """
    import cv2
    import numpy as np
    from pathlib import Path
    img = cv2.imread(str(patch))
    if img is None:
        raise ValueError(f'не прочитана заплатка: {patch}')
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    now = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    applied = now < sharp * 0.95
    if applied:
        blur = cv2.GaussianBlur(img, (3, 3), 0.8)
        img = np.clip(img.astype('float32') * 1.5 - blur.astype('float32') * 0.5,
                      0, 255).astype('uint8')
        now = float(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY),
                                  cv2.CV_64F).var())
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    return {'sharp': round(now, 1), 'want': sharp, 'applied': applied}


def ai_look_integrity(composite, source, box):
    """Целостность composite: вне выкройки — байт-в-байт, на шве — по перу.

    Плечо «обрезано» означает: граница выкройки перерисовала то, что должно
    быть нетронутым, и скачок шва видно глазом. Инструмент отвечает цифрами:
    max вне бокса (должен быть 0 — вне бокса оригинал не тронут) и скачок на
    самой границе (перо его сглаживает, большой — бокс резал тело).
    """
    import cv2
    import numpy as np
    comp = cv2.imread(str(composite))
    src = cv2.imread(str(source))
    if comp is None or src is None:
        raise ValueError('не прочитана пара кадров для integrity')
    x0, y0, x1, y1 = (int(v) for v in box)
    d = np.abs(comp.astype('int16') - src.astype('int16'))
    outside = 0
    h, w = comp.shape[:2]
    if y0:
        outside = max(outside, int(d[:y0].max()))
    if y1 < h:
        outside = max(outside, int(d[y1:].max()))
    if x0:
        outside = max(outside, int(d[:, :x0].max()))
    if x1 < w:
        outside = max(outside, int(d[:, x1:].max()))
    seam = int(d[max(0, y0 - 2):y0, :].max()) if y0 else 0
    return {'outside': outside, 'seam': seam}


def ai_look_parts(path, box):
    """Человеческим глазом не видно, а vision скажет: что в кадре, кроме лица.

    Пальцы у подбородка, лямка, чужая рука — прямоугольник выкройки всегда
    задевает что-то ниже лица; список этих вещей решает зовущему (сузить
    выкройку, смириться с перерисовкой), а не спорить с глазами.
    """
    from pathlib import Path
    from PIL import Image
    import io
    from ai.ai_vision import ai_vision_describe_wait
    im = Image.open(path).convert('RGB')
    if box:
        im = im.crop(tuple(int(v) for v in box))
    buf = io.BytesIO()
    im.save(buf, 'JPEG')
    ans = ai_vision_describe_wait(
        buf.getvalue(),
        'Что на кадре есть, кроме лица: перечисли части тела и одежду одним '
        'словом каждое, списком через запятую; если ничего нет — ответь «ничего».')
    return [w.strip(' .') for w in ans.lower().replace('\n', ',').split(',')
            if w.strip(' .') and w.strip(' .') != 'ничего']


def ai_look_anchor(patches, source, det_size=(320, 320), top=8):
    """Выбрать патч лица персоны под конкретный source-кадр: ЕЁ лицо и в ракурсе кадра.

    Ранг гибрид (AI_LOOK_RANK): доля к косинусу против лица самого кадра —
    платч нужен ближайший к нему; доля к центроиду всех патчей (датасет может
    быть не про одно лицо) — один косинуc к кадру выбрал бы смазанный/грязный
    патч там, где у исходника асимметрия. Ещё множители: похожесть ракурса
    (_look_pose) и цветность (чб ниже). Из лидеров ранга спрашиваем
    ai_look_parts: якорь — первый без рук (AI_LOOK_ANCHOR_HANDS); везде руки —
    берём ранг выше всех как есть.
    """
    import numpy as np
    from pathlib import Path
    from ai.ai_face import AI_FACE_MODEL, _face_app
    cv2, app = _face_app(AI_FACE_MODEL, tuple(det_size))
    big = lambda fs: max(fs, key=lambda x: (x.bbox[2] - x.bbox[0]) *
                         (x.bbox[3] - x.bbox[1]))
    found = []
    for p in map(Path, patches):
        img = cv2.imread(str(p))
        if img is None:
            continue
        fs = app.get(img)
        if not fs:
            continue
        f = big(fs)
        x0, y0, x1, y1 = (int(v) for v in f.bbox)
        core = img[y0:y1, x0:x1].astype('int16')
        sat = float(np.median(core.max(axis=2) - core.min(axis=2)))
        found.append((p, f, sat))
    if not found:
        raise ValueError('ни на одном патче не нашлось лица')
    center = np.mean([f.normed_embedding for _, f, _ in found], axis=0)
    center = center / np.linalg.norm(center)
    sface = big(app.get(cv2.imread(str(source))))
    s_emb = sface.normed_embedding
    ps = _look_pose(sface.kps, sface.bbox)
    cand = [(round((AI_LOOK_RANK[0] * float(f.normed_embedding @ s_emb) +
                    AI_LOOK_RANK[1] * float(f.normed_embedding @ center)) *
                   max(0.2, 1 - _look_pose_dist(_look_pose(f.kps, f.bbox), ps)) *
                   min(1.0, sat / 40), 4), p, [int(v) for v in f.bbox])
            for p, f, sat in found]
    cand.sort(key=lambda c: c[0], reverse=True)
    best = None
    for rank, p, box in cand[:top]:
        parts = ai_look_parts(p, box)
        if best is None:
            best = {'face': str(p), 'rank': rank, 'box': box, 'parts': parts}
        if not any(any(k in w for k in AI_LOOK_ANCHOR_HANDS) for w in parts):
            return {'face': str(p), 'rank': rank, 'box': box, 'parts': parts}
    return best


def _look_color_name(rgb):
    """Имя тона волос — ближайший эталон AI_LOOK_HAIR_COLORS."""
    best, best_d = 'brown', 1e9
    for name, ref in AI_LOOK_HAIR_COLORS.items():
        d = sum((a - b) ** 2 for a, b in zip(rgb, ref))
        if d < best_d:
            best, best_d = name, d
    return best


def _look_pose(kps, box):
    """Ракурс лица по пяти точкам детектора: наклон головы, поворот, подъём.

    Наклон — угол линии глаз; поворот — насколько нос ушёл от середины глаз
    (в профиль линия сжимается, нос ползёт к краю); подъём — где линия глаз
    стоит в коробке лица (задранный подбородок — глаза ниже центра).
    """
    import math
    (lx, ly), (rx, ry) = kps[0][:2], kps[1][:2]
    roll = math.degrees(math.atan2(float(ry - ly), float(rx - lx)))
    yaw = float(kps[2][0] - (lx + rx) / 2) / max(1.0, float(abs(rx - lx)))
    pitch = (float(ly + ry) / 2 - float(box[1])) / max(1.0, float(box[3] - box[1]))
    return round(roll, 1), round(yaw, 3), round(pitch, 3)


def _look_pose_dist(a, b):
    """Рассогласование ракурсов: слагаемые нормированы по AI_LOOK_POSE."""
    return min(1.0, abs(a[0] - b[0]) / AI_LOOK_POSE[0] +
               abs(a[1] - b[1]) / AI_LOOK_POSE[1] +
               abs(a[2] - b[2]) / AI_LOOK_POSE[2])


def _look_core(px):
    """Пиксели в межквартильном диапазоне яркости (25–75%): тень, блик и помада
    в истинный тон не входят; тот же фильтр — у probe и у ai_look_fit, чтобы
    аффина сверялась с одним и тем же ядром."""
    import numpy as np
    if len(px) < 8:
        return px
    y = px @ np.array([0.114, 0.587, 0.299], 'float32')
    lo, hi = np.percentile(y, [25, 75])
    return px[(y >= lo) & (y <= hi)]

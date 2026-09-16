"""
Замеры кадра для самоуверенной заплатки: кожа, волосы, свет, резкость, лишнее.

Лицо натянули — претензии к кадру принимаются по цифрам, не глазами: кожа не
та, волосы не того цвета, плечо обрезано, лицо мыльное. Чтобы править самим,
у кадра сначала спрашиваем (probe): какой тон кожи у оригинала, какой цвет и
доля волос в выкройке, какой свет, какая резкость. Промпт графа собирается из
замеров (prompt) — модель рисует то, что на кадре есть, а не что ей показалось.
Заплатку после генерации подтягиваем к замерам: цвет (fit), резкость (sharpen).
Целостность composite после вшивки — это integrity: вне выкройки кадр обязан
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
# Низ 0.68, а не 0.8: на текстурно-плотной заплатке фактический std_s/std_gen
# 0.47–0.60 (ядро лица плотнее полосы тела), клип 0.8 душил подачу и оставлял
# чужую структуру кожи (замер 2026-09-15: 0.711 против 0.695).
AI_LOOK_FIT_CLIP = (0.68, 1.25)


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


def ai_look_fit(patch, probe, out, region=None, full=0.0, weighted=True):
    """Подтянуть цвет заплатки к замерам оригинала; {'before','after','moved'}.

    Комящая заплатка помнит цвет датасета: кожа светлее, каналы растянуты не
    так (синий тонет). Канальная аффина New=(Gen−mean_gen)·std_src/std_gen+
    mean_src по тому же межквартильному ядру, что и probe: и сдвиг, и масштаб
    канала; множитель запутан клипом [AI_LOOK_FIT_CLIP] — на плоской заплатке
    std_gen≈0 не должен разгонять шум. Без probe['skin_std'] — сдвиг, без
    масштаба (обратная совместимость). `region` (x0,y0,x1,y1) — где мерить
    каналы патча (например, полоса кожи тела под лицом); без него — центральное
    ядро кадра.

    `full` — сколько ОСТАВШЕГОСЯ рассогласования доправить сверх гейта: 0 —
    как было (только избыток), 1 — полный сдвиг к цели. Гейт бережёт сходство,
    но оставляет лицо теплее соседней кожи (замер 3095684: минус 26 по синему
    против настоящей кожи рядом), а на кадре, где лицо и тело в одном свете,
    это видно глазом раньше цифры.

    Правится ИЗБЫТОК рассогласования, а не весь он: coef = max(D−d_lo, 0)/D,
    где D = |mu_s − mu_g| — величина сдвига, d_lo = разброс ядра (тень и блик
    лица — тоже лицо). Пока D ≤ d_lo свет гена уже в тоне цели и аффине почти
    молчит (лора второго прохода кладёт свет сама; замер 2026-09-15: полный
    сдвиг тянул готовое лицо к полосе тела — 0.695 против 0.711 с гейтом).

    `weighted` — взвешивать ли правку по «лицевости». Карта нужна ровно тогда,
    когда цель взята с ДРУГОЙ ткани (кожа тела под лицом): она бережёт волосы и
    фон, попавшие в заплатку. Если же цель — кожа самого лица, взвешивать нечего,
    а карта режет лицо на «близкие к среднему» и «далёкие» пиксели и красит его
    пятнами: тени, губы и глаза далеки от среднего по построению (замер
    2026-09-16, кадр 1306a4ca — жёлто-зелёные кляксы при верном сдвиге).

    Аффина применяется к пикселю ПРОПОРЦИОНАЛЬНО его «лицевости», а не ковром:
    w = клип((D − |x − mu_g|)/(D − d_lo)). Ковёр был причиной «синих квадратов»
    (замер 2026-09-14: фон вне бокса [221,215,212] против сдвинутого внутри
    [214,221,250] — шаг B+38 по периметру). Делимость на 0 при D≈0: сдвигать
    нечего, вес не нужен.
    """
    import cv2
    import numpy as np
    img = cv2.imread(str(patch))
    if img is None:
        raise ValueError(f'не прочитана заплатка: {patch}')
    h, w = img.shape[:2]
    sub = (img[int(region[1]):int(region[3]), int(region[0]):int(region[2])]
           if region else
           img[int(h * 0.3):int(h * 0.85), int(w * 0.25):int(w * 0.75)])
    core = _look_core(sub.reshape(-1, 3).astype('float32'))
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
    f = img.astype('float32')
    shift = mu_s - mu_g
    D = float(np.linalg.norm(shift))
    d_lo = float(np.linalg.norm(sd_g))
    x = f - mu_g
    # Правится только ИЗБЫТОК рассогласования сверх естественного разброса ядра.
    # Когда ген уже стоит в свете (D ≤ d_lo — лицо гена в тоне цели), полный сдвиг
    # тянул готовое лицо к полосе тела и портил якорный косинус (замер 2026-09-15:
    # D≈82 при d_lo≈72 — гейт 0.711 против 0.695 полного сдвига; лора второго
    # прохода кладёт свет сама, аффине дорабатывает то, что осталось).
    coef = max(D - d_lo, 0.0) / max(D, 1e-9)
    coef = coef + (1.0 - coef) * max(0.0, min(1.0, full))
    if weighted and D > d_lo:
        # вес пикселя: 1 на лице (d≈0), 0 на коже тела (d≈D) и на волосах/фоне
        d = np.linalg.norm(x, axis=2)
        w = np.clip((D - d) / max(D - d_lo, 1.0), 0, 1)[..., None]
    else:
        # Сдвиг меньше собственного разброса ядра: «далёких от лица» пикселей
        # просто нет, и карта веса вырождается в резкие пятна 0/1 по признаку
        # «пиксель почти точно равен среднему». Именно так лицо покрывалось
        # жёлто-зелёными кляксами при D≈2 и d_lo≈40 (замер 2026-09-16, кадр
        # 1306a4ca). Правим тогда равномерно — сдвигать всё равно почти нечего.
        w = 1.0
    img = np.clip(f + (x * (coef * (ratio - 1)) + shift * coef) * w,
                  0, 255).astype('uint8')
    from pathlib import Path
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    return {'before': before, 'after': probe['skin'], 'coef': round(coef, 3),
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


def ai_look_anchor(patches, source, det_size=(320, 320), top=8, parts=True,
                   cache=''):
    """Выбрать патч лица персоны под конкретный source-кадр: ЕЁ лицо и в ракурсе кадра.

    Ранг гибрид (AI_LOOK_RANK): доля к косинусу против лица самого кадра —
    патч нужен ближайший к нему; доля к центроиду всех патчей (датасет может
    быть не про одно лицо) — один косинус к кадру выбрал бы смазанный/грязный
    патч там, где у исходника асимметрия. Ещё множители: похожесть ракурса
    (_look_pose) и цветность (чб ниже). Из лидеров ранга спрашиваем
    ai_look_parts: якорь — первый без рук (AI_LOOK_ANCHOR_HANDS); везде руки —
    берём ранг выше всех как есть.

    `cache` — файл с замерами пачки (эмбединг, точки, коробка, цветность на
    файл). Замеры патча от кадра не зависят, а детекция двух сотен патчей на CPU
    идёт минутами — и платил их КАЖДЫЙ прогон swap, хотя пачка не менялась.
    Новые файлы дописываются в кэш, пропавшие просто не читаются.
    """
    import numpy as np
    from pathlib import Path
    from ai.ai_face import AI_FACE_MODEL, _face_app
    # пять точек отдаёт сам детектор: 2d106, 3d68 и пол-возраст здесь лишние,
    # а на пачке в две сотни патчей их прогон и есть всё время ранжирования
    cv2, app = _face_app(AI_FACE_MODEL, tuple(det_size),
                        modules=('detection', 'recognition'))
    big = lambda fs: max(fs, key=lambda x: (x.bbox[2] - x.bbox[0]) *
                         (x.bbox[3] - x.bbox[1]))
    import json
    known = {}
    if cache and Path(cache).exists():
        known = json.loads(Path(cache).read_text())
    found, fresh = [], False
    for p in map(Path, patches):
        got = known.get(p.name)
        if got is None:
            img = cv2.imread(str(p))
            if img is None:
                continue
            fs = app.get(img)
            if not fs:
                continue
            f = big(fs)
            x0, y0, x1, y1 = (int(v) for v in f.bbox)
            core = img[y0:y1, x0:x1].astype('int16')
            got = {'emb': [round(float(v), 5) for v in f.normed_embedding],
                   'kps': [[float(a), float(b)] for a, b in f.kps],
                   'bbox': [int(v) for v in f.bbox],
                   'sat': float(np.median(core.max(axis=2) - core.min(axis=2)))}
            known[p.name], fresh = got, True
        found.append((p, got))
    if not found:
        raise ValueError('ни на одном патче не нашлось лица')
    if cache and fresh:
        Path(cache).write_text(json.dumps(known, ensure_ascii=False) + '\n')
    center = np.mean([np.asarray(g['emb'], 'float32') for _, g in found], axis=0)
    center = center / np.linalg.norm(center)
    sface = big(app.get(cv2.imread(str(source))))
    s_emb = sface.normed_embedding
    ps = _look_pose(sface.kps, sface.bbox)
    cand = [(round((AI_LOOK_RANK[0] * float(np.asarray(g['emb'], 'float32') @ s_emb) +
                    AI_LOOK_RANK[1] * float(np.asarray(g['emb'], 'float32') @ center)) *
                   max(0.2, 1 - _look_pose_dist(_look_pose(g['kps'], g['bbox']), ps)) *
                   min(1.0, g['sat'] / 40), 4), p, g['bbox'])
            for p, g in found]
    cand.sort(key=lambda c: c[0], reverse=True)
    if not parts:
        # без vision: ранг решает всё. Руки у подбородка отсеять нечем, зато
        # прогон не требует ключа провайдера — годится, когда якорь проверяют
        # глазами или берут из уже отобранного каталога.
        rank, p, box = cand[0]
        return {'face': str(p), 'rank': rank, 'box': box, 'parts': []}
    best = None
    for rank, p, box in cand[:top]:
        got = ai_look_parts(p, box)
        if best is None:
            best = {'face': str(p), 'rank': rank, 'box': box, 'parts': got}
        if not any(any(k in w for k in AI_LOOK_ANCHOR_HANDS) for w in got):
            return {'face': str(p), 'rank': rank, 'box': box, 'parts': got}
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
    """Ракурс лица: та же формула, что у `ai_landmark_pose` — и она там одна."""
    from ai.ai_landmark import ai_landmark_pose
    return ai_landmark_pose(kps, box)


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

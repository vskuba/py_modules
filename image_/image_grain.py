"""
Зерно и фокус: почему заплатка «нарисована», даже когда лицо правильное.

Генератор отдаёт стерильный пиксель. Тон подогнан, геометрия верна, шов не
виден — а глаз всё равно говорит «нарисовано», потому что рядом лежит
настоящая кожа с шумом матрицы, следами JPEG и порами, а на заплатке гладко.
Разница меряется числом: сигма высокочастотного остатка (оценка Иммеркера —
одна свёртка, устойчива к структуре) и дисперсия Лапласиана как резкость.

Зерно не синтезируется белым шумом, а БЕРЁТСЯ С КАДРА: остаток настоящей кожи
рядом с лицом несёт спектр этой камеры и этого сжатия, а белый шум ложится
ровным песком и читается так же фальшиво, как гладкость. Не нашлось эталонной
области — падаем на гауссов шум со слабой корреляцией и говорим об этом
вердиктом.

Резкость доводится только ВНИЗ (размытием): unsharp наверх рисует ореолы по
контуру, и они — такой же признак подделки, как гладкость.
"""
import argparse
import json

# Ядро Иммеркера: оценка сигмы шума одной свёрткой, нечувствительная к плавной
# структуре кадра (лицо, градиент света) — реагирует на пиксельный разброс.
IMAGE_GRAIN_KERNEL = ((1, -2, 1), (-2, 4, -2), (1, -2, 1))
# Насколько сигма заплатки должна отстать от эталона, чтобы вообще досыпать
# зерно: ниже этого — разница глазу не видна, а шум добавлять вредно.
IMAGE_GRAIN_EPS = 0.15
# Радиус размытия при подгонке резкости вниз, пиксели.
IMAGE_GRAIN_BLUR = 0.6
# На сколько единиц яркости эталон может отойти от лица, прежде чем его ранг
# упадёт вдвое: кожа в другом свете — это и другой тон, и другое зерно.
IMAGE_GRAIN_LUMA = 25.0
# Коридор яркости «это ещё кожа», долями от яркости кожи лица: волосы темнее
# нижней границы, пересвет и фон — выше верхней.
IMAGE_GRAIN_SKIN_BAND = (0.62, 1.7)
# Радиус высокочастотного среза при выемке зерна из кадра: крупнее — в остаток
# попадает структура, которую плитка тиражирует узором.
IMAGE_GRAIN_HIGHPASS = 0.8
# Сколько робастных сигм остатка считать зерном: выше — это уже не шум, а край.
IMAGE_GRAIN_CLIP = 2.5
# Нижняя граница фактуры эталона, долей от сигмы самого лица: гладкая стена
# проходит и по цветности, и по яркости (замер istockphoto-653141840: фон кухни
# sharp 2.8, сигма 0.22 против 0.93 у лица), но зерна в ней нет — и «подгонка»
# по ней равняет лицо на пустое место.
IMAGE_GRAIN_REF_MIN = 0.45
# Полурадиус ядра синтеза, px: корреляция зерна короткая (демозаик и блок JPEG),
# дальше в ядро попадает структура кадра.
IMAGE_GRAIN_KERNEL_R = 6
# Зерно синтезируется от фиксированного семени: один и тот же кадр, прогнанный
# дважды, обязан дать один и тот же файл — иначе нечего сравнивать между осями.
IMAGE_GRAIN_SEED = 20260915


def image_grain_measure(path, box=None, mask='', scale_to=0):
    """Замер фактуры области; {'sigma','sharp','luma','n','box','scale'}.

    `sigma` — оценка шума по Иммеркеру (по каналам BGR), `sharp` — дисперсия
    Лапласиана серого, `luma` — медианная яркость, `n` — сколько пикселей
    участвовало. `box` (x0,y0,x1,y1) режет область, `mask` — файл серой маски:
    считаются только пиксели, где маска светлее половины (например «настоящая
    кожа тела рядом с лицом», а не сам подменённый овал).

    `box` и `scale` возвращаются намеренно: **мерка обязана говорить, ГДЕ и в
    каком масштабе мерила**. Число без этого не проверить глазами, а проверять
    приходится — окно замера однажды полгода стояло не на щеке, а на глазу, и
    поймалось только рисованием (`image_hotspot_overlay(boxes=…)`).

    ⚠⚠ `scale_to` — привести окно к этой ширине перед счётом. Без него
    сравнивать замеры с кадров разного разрешения НЕЛЬЗЯ: и `sigma`, и `sharp`
    считают частоту в пикселях, и одна и та же кожа на крупном кадре даёт
    другое число просто потому, что растянута на больше пикселей. Замер, на
    котором это поймано: одно и то же лицо дало 345 на рождении 1024 и 180 на
    2048 — разница целиком от масштаба.

    ⚠ Приведение только УМЕНЬШАЕТ. Растянутое окно меряет интерполяцию, а не
    кожу: лицо 115 px, раздутое до 512, даёт `sharp` 10.2 — число ни о чём.
    Окно мельче `scale_to` остаётся как есть и помечается `small`.
    """
    import cv2
    import numpy as np
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f'не прочитан кадр: {path}')
    if box:
        x0, y0, x1, y1 = (int(v) for v in box)
        h, w = img.shape[:2]
        img = img[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    if img.size == 0:
        raise ValueError(f'область пуста: {box}')
    sel = None
    if mask:
        m = cv2.imread(str(mask), cv2.IMREAD_GRAYSCALE)
        if m is None:
            raise ValueError(f'не прочитана маска: {mask}')
        if box:
            x0, y0, x1, y1 = (int(v) for v in box)
            m = m[max(0, y0):y1, max(0, x0):x1]
        sel = m > 127
        if not sel.any():
            raise ValueError('под маской нет пикселей для замера')
    # Приведение к общей мерке — ДО счёта и только вниз (см. докстринг).
    # Маска едет тем же множителем, иначе выбор пикселей разъедется с кадром.
    small, k = False, 1.0
    if scale_to:
        k = float(scale_to) / max(1, img.shape[1])
        if k < 1.0:
            img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            if sel is not None:
                sel = cv2.resize(sel.astype('uint8'), (img.shape[1], img.shape[0]),
                                 interpolation=cv2.INTER_NEAREST) > 0
                if not sel.any():
                    raise ValueError('после приведения под маской не осталось пикселей')
        else:
            small, k = True, 1.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Резкость тоже считается ПОД МАСКОЙ: волосы и край внутри бокса дают
    # лапласиан в разы выше кожи, и без маски «резкость лица» оказывается
    # резкостью причёски (замер 3095684: 325 по боксу против 130 по коже).
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    got = {'sigma': [round(_grain_sigma(img[..., c], sel), 2) for c in range(3)],
           'sharp': round(float((lap[sel] if sel is not None else lap).var()), 1),
           'luma': round(float(np.median(gray[sel] if sel is not None else gray)), 1),
           'n': int(sel.sum()) if sel is not None else int(gray.size),
           'box': [int(v) for v in box] if box else None,
           'scale': round(k, 3)}
    if small:
        got['small'] = True     # окно мельче мерки — сравнивать не с чем
    return got


def image_grain_match(patch, out, want, ref='', ref_box=None, strength=1.0):
    """Довести фактуру заплатки до эталона; {'before','after','added','blurred'}.

    `want` — словарь замера эталона (`image_grain_measure` настоящей кожи
    кадра). Если заплатка глаже — на неё кладётся зерно, вырезанное из `ref`
    (кадр) по `ref_box`: остаток этой области после лёгкого размытия несёт
    спектр камеры и сжатия. Без `ref` — гауссов шум со слабой корреляцией,
    в вердикте `added: 'гаусс'`. Если заплатка РЕЗЧЕ эталона — лёгкое
    размытие (вверх не точим: unsharp даёт ореолы).

    `strength` — множитель зерна: 1.0 ровно до эталона, меньше — осторожнее.
    """
    import cv2
    import numpy as np
    from pathlib import Path
    img = cv2.imread(str(patch))
    if img is None:
        raise ValueError(f'не прочитана заплатка: {patch}')
    before = image_grain_measure(patch)
    h, w = img.shape[:2]
    f = img.astype('float32')
    blurred = False
    if want.get('sharp') and before['sharp'] > want['sharp'] * 1.25:
        f = cv2.GaussianBlur(f, (0, 0), IMAGE_GRAIN_BLUR)
        blurred = True
    added = ''
    noise = None
    for c in range(3):
        # клип перед uint8: numpy сворачивает выход за 255 по модулю, и один
        # пересвеченный пиксель дал бы чёрный — то есть ложную «сигму»
        have = _grain_sigma(np.clip(f[..., c], 0, 255).astype('uint8'))
        gap = float(want['sigma'][c]) - have
        if gap <= IMAGE_GRAIN_EPS:
            continue
        need = float(np.sqrt(max(0.0, want['sigma'][c] ** 2 - have ** 2))) * strength
        if noise is None:
            noise, added = _grain_source(ref, ref_box, h, w)
        n = noise[..., c]
        # масштаб — по ТОЙ ЖЕ оценке, что и цель: зерно кадра коррелировано
        # (соседние пиксели связаны демозаиком и JPEG), и его std больше
        # иммеркеровой сигмы в разы — по std заплатка недобрала бы вдвое.
        sd = _grain_sigma(n) or 1.0
        f[..., c] += n * (need / sd)
    img = np.clip(f, 0, 255).astype('uint8')
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    after = image_grain_measure(out)
    return {'before': before, 'after': after, 'want': want,
            'added': added, 'blurred': blurred}


def image_grain_ref(photo, box, skin=None):
    """Найти рядом с лицом окно НАСТОЯЩЕЙ кожи; {'box','skin_part','sharp'}.

    Эталон зерна нельзя брать наугад: под подбородком волосы, на груди цепочка,
    за плечом вода — сигма там про что угодно, только не про кожу. Окна
    перебираются вокруг выкройки (ниже — шея и грудь, по бокам — щека и плечо),
    и берётся то, где больше пикселей похожи на тон кожи лица (`skin` — BGR
    медиана; без неё считается по ядру самой выкройки) и меньше структуры.

    Возвращает лучшее окно. `skin_part` — какая доля его пикселей похожа на
    кожу, `flat` — окно прошло по цвету, но фактуры в нём нет (гладкий фон),
    `own` — кожи рядом не нашлось вовсе и эталоном стало ядро самого лица.
    Функция НЕ БРОСАЕТ исключение из-за отсутствия кожи: на крупном портрете
    вокруг лица её и не бывает, а ронять из-за этого весь прогон нельзя.
    """
    import cv2
    import numpy as np
    img = cv2.imread(str(photo))
    if img is None:
        raise ValueError(f'не прочитан кадр: {photo}')
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in box)
    bw, bh = x1 - x0, y1 - y0
    if skin is None:
        core = img[y0 + int(bh * .3):y0 + int(bh * .85),
                   x0 + int(bw * .25):x1 - int(bw * .25)].reshape(-1, 3)
        skin = np.median(core.astype('float32'), axis=0) if core.size else np.zeros(3)
    skin = np.asarray(skin, 'float32')
    like = _grain_skin_like(img, skin)
    # Яркость КОЖИ лица, а не выкройки: в выкройке половина пикселей — волосы
    # и тень, её медиана (48 у 3095684) вдвое ниже кожи (79) и увела бы поиск
    # эталона прямо в причёску.
    face_luma = float(np.asarray(skin, 'float32') @ np.float32([.114, .587, .299]))
    ww, wh = max(12, int(bw * .45)), max(12, int(bh * .25))
    step = max(4, min(ww, wh) // 2)
    # кольцо поиска: вокруг выкройки на её ширину, само лицо исключено
    sx0, sy0 = max(0, x0 - bw), max(0, y0 - bh // 2)
    sx1, sy1 = min(w, x1 + bw), min(h, y1 + bh)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Сколько шума у самого лица: эталон беднее этого — не кожа, а гладкий фон
    face_sigma = _grain_sigma(gray[y0 + int(bh * .3):y0 + int(bh * .85),
                                   x0 + int(bw * .25):x1 - int(bw * .25)])
    floor = face_sigma * IMAGE_GRAIN_REF_MIN
    best, best_flat = None, None
    for b in range(sy0, sy1 - wh + 1, step):
        for a in range(sx0, sx1 - ww + 1, step):
            c, d = a + ww, b + wh
            if a < x1 and c > x0 and b < y1 and d > y0:
                continue                      # пересекает выкройку — не эталон
            part = float(like[b:d, a:c].mean())
            if part < 0.5:
                continue
            sig = _grain_sigma(gray[b:d, a:c])
            sharp = float(cv2.Laplacian(gray[b:d, a:c], cv2.CV_64F).var())
            luma = float(np.median(gray[b:d, a:c]))
            # Три требования к эталону, все три — делители ранга:
            # кожа (part), без структуры (волосы, цепочка, край — это не зерно)
            # и В ТОМ ЖЕ СВЕТЕ: освещённая рука при лице в тени даёт и чужой
            # тон, и чужое зерно — в тенях JPEG шумит иначе, чем в светах.
            rank = (part / (1 + sharp / 500)
                    / (1 + abs(luma - face_luma) / IMAGE_GRAIN_LUMA))
            got = (rank, [a, b, c, d], round(part, 3), round(sharp, 1),
                   round(luma, 1), round(sig, 2))
            if sig < floor:                   # гладкий фон: кожи тут нет
                if best_flat is None or rank > best_flat[0]:
                    best_flat = got
                continue
            if best is None or rank > best[0]:
                best = got
    flat = best is None
    best = best or best_flat
    if best is None:
        # Кожи рядом нет вовсе — обычное дело на крупном портрете: вокруг лица
        # только волосы и фон (замер: 18 кадров из 31 в тестовом наборе). Это
        # не авария: источником зерна становится ЯДРО САМОГО ЛИЦА — та же
        # камера, то же сжатие, та же плотность деталей. Структуру оттуда
        # вычищает высокочастотный срез с клипом хвостов.
        core = [x0 + int(bw * .25), y0 + int(bh * .3),
                x1 - int(bw * .25), y0 + int(bh * .85)]
        return {'box': core, 'skin_part': 1.0, 'sharp': 0.0,
                'luma': round(face_luma, 1), 'sigma': round(face_sigma, 2),
                'flat': False, 'own': True,
                'face_sigma': round(face_sigma, 2), 'face_luma': round(face_luma, 1)}
    return {'box': best[1], 'skin_part': best[2], 'sharp': best[3],
            'luma': best[4], 'sigma': best[5], 'flat': flat, 'own': False,
            'face_sigma': round(face_sigma, 2), 'face_luma': round(face_luma, 1)}


def _grain_skin_like(img, skin):
    """Булева карта «похоже на кожу»: по ЦВЕТНОСТИ, а не по яркости.

    Кожа руки на солнце и кожа лица в тени различаются яркостью заметно, но
    доли каналов у них близки — поэтому сравниваются нормированные r и b (доля
    канала в сумме).

    Одной цветности мало: у каштановых волос она ПОЧТИ ТА ЖЕ, что у тёплой
    кожи, — их делит только яркость (замер 3095684: волосы [55,37,27] прошли
    проверку по цветности и увели эталон в причёску). Поэтому яркость пикселя
    обязана лежать в коридоре вокруг яркости кожи: волосы вдвое темнее, блик
    вдвое светлее — оба мимо.
    """
    import numpy as np
    f = img.astype('float32')
    s = f.sum(axis=2) + 1e-6
    sk = np.asarray(skin, 'float32')
    ss = float(sk.sum()) + 1e-6
    d = (np.abs(f[..., 2] / s - sk[2] / ss) + np.abs(f[..., 0] / s - sk[0] / ss))
    w = np.float32([.114, .587, .299])
    luma, skin_luma = f @ w, max(1.0, float(sk @ w))
    return ((d < 0.045) & (luma > skin_luma * IMAGE_GRAIN_SKIN_BAND[0])
            & (luma < skin_luma * IMAGE_GRAIN_SKIN_BAND[1]))


def _grain_sigma(chan, sel=None):
    """Сигма шума канала по Иммеркеру: |I ⊛ L| со скидкой на ожидание модуля."""
    import cv2
    import numpy as np
    k = np.asarray(IMAGE_GRAIN_KERNEL, 'float32')
    r = np.abs(cv2.filter2D(chan.astype('float32'), -1, k))
    if sel is not None:
        r = r[sel]
    if not r.size:
        return 0.0
    # sqrt(pi/2)/6 — нормировка ядра к сигме белого шума
    return float(np.sqrt(np.pi / 2) / 6 * r.mean())


def _grain_kernel(res):
    """Ядро свёртки с тем же амплитудным спектром, что у остатка эталона.

    Белый шум плоский по спектру; свернув его с таким ядром, получаем процесс
    со спектром настоящего зерна — то есть с его длиной корреляции и следом
    сетки JPEG, но со случайной фазой. Ядро берётся центральным окном
    (`IMAGE_GRAIN_KERNEL_R`): корреляция зерна короткая, хвосты ядра — это уже
    структура кадра.
    """
    import numpy as np
    k = np.fft.fftshift(np.fft.irfft2(np.abs(np.fft.rfft2(res)), s=res.shape))
    cy, cx = k.shape[0] // 2, k.shape[1] // 2
    r = min(IMAGE_GRAIN_KERNEL_R, cy, cx)
    ker = k[cy - r:cy + r + 1, cx - r:cx + r + 1].astype('float32')
    return ker / (float(np.sqrt((ker ** 2).sum())) or 1.0)


def _grain_source(ref, ref_box, h, w):
    """Плитка зерна размером (h,w): остаток настоящей кожи кадра или гаусс."""
    import cv2
    import numpy as np
    if ref:
        img = cv2.imread(str(ref))
        if img is not None and ref_box:
            x0, y0, x1, y1 = (int(v) for v in ref_box)
            ih, iw = img.shape[:2]
            crop = img[max(0, y0):min(ih, y1), max(0, x0):min(iw, x1)]
            if crop.size and min(crop.shape[:2]) >= 8:
                res = crop.astype('float32') - cv2.GaussianBlur(
                    crop.astype('float32'), (0, 0), IMAGE_GRAIN_HIGHPASS)
                # Из остатка вычищается СТРУКТУРА: волосок, кромка, край блика
                # дают редкие большие значения, и плитка тиражирует их узором
                # (замер 3095684: ромбическая сетка на щеке от зеркального
                # блока). Зерно — это середина распределения, поэтому хвосты
                # за 2.5 робастных сигмы срезаются, а шум остаётся целым.
                sd = 1.4826 * np.median(np.abs(res - np.median(res))) or 1.0
                res = np.clip(res, -IMAGE_GRAIN_CLIP * sd, IMAGE_GRAIN_CLIP * sd)
                # Зерно не ТИРАЖИРУЕТСЯ, а СИНТЕЗИРУЕТСЯ по спектру эталона:
                # белый шум фильтруется ядром, у которого тот же амплитудный
                # спектр, что у настоящего остатка. Спектр и есть характер
                # зерна (длина корреляции, след блоков JPEG), а фаза случайна —
                # поэтому ни стыков, ни повторов. Обе плитки до этого провалились
                # на замере: повтором — прямая линия на стыке (x=68), зеркальная
                # — калейдоскоп из ромбов на щеке (симметрия сама рождает узор).
                rng = np.random.default_rng(IMAGE_GRAIN_SEED)
                tile = np.empty((h, w, 3), 'float32')
                for c in range(3):
                    ker = _grain_kernel(res[..., c])
                    tile[..., c] = cv2.filter2D(
                        rng.normal(0, 1, (h, w)).astype('float32'), -1, ker)
                return tile, 'кадр'
    rng = np.random.default_rng(12345)
    n = rng.normal(0, 1, (h, w, 3)).astype('float32')
    return cv2.GaussianBlur(n, (0, 0), 0.5), 'гаусс'


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='image_grain',
                                 description='замер и подгонка зерна/фокуса')
    ap.add_argument('command', choices=['measure', 'match'])
    ap.add_argument('path', help='кадр (measure) или заплатка (match)')
    ap.add_argument('--out', default='', help='match: куда положить результат')
    ap.add_argument('--box', default='', help='измеряемая область «x0,y0,x1,y1»')
    ap.add_argument('--mask', default='', help='measure: считать под маской')
    ap.add_argument('--ref', default='', help='match: кадр-источник зерна')
    ap.add_argument('--ref-box', default='', help='match: область кожи в кадре')
    ap.add_argument('--strength', type=float, default=1.0)
    ns = ap.parse_args()
    box = [int(v) for v in ns.box.split(',')] if ns.box else None
    try:
        if ns.command == 'measure':
            print(json.dumps(image_grain_measure(ns.path, box, ns.mask),
                             ensure_ascii=False))
        else:
            if not (ns.out and ns.ref_box):
                raise SystemExit('match требует --out и --ref-box (эталон кожи)')
            rb = [int(v) for v in ns.ref_box.split(',')]
            want = image_grain_measure(ns.ref or ns.path, rb)
            print(json.dumps(image_grain_match(ns.path, ns.out, want, ns.ref, rb,
                                               ns.strength), ensure_ascii=False))
    except (ValueError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')

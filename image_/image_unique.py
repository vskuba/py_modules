"""Насколько кадр отличим от исходного: перцептивные хеши и точки-приметы.

Зачем: после подмены лица кадр — производная чужой фотографии, и вопрос «узнают
ли в нём исходник» решается не на глаз. Обработку подбирают по двум числам
сразу, потому что они тянут в разные стороны: отличие растёт вместе с порчей
кадра, и без второго числа первое всегда можно довести до предела.

Что меряем:

* **хеши** — `aHash` (средняя яркость), `dHash` (перепад соседних), `pHash`
  (низкие частоты DCT). По 64 бита, сравниваются расстоянием Хэмминга. Это то,
  чем ищут дубликаты в больших базах, и первое, обо что спотыкается «то же фото,
  чуть подкрашенное»;
* **точки** — доля совпавших ORB-дескрипторов. Хеш смотрит на кадр целиком и
  слепнет от кропа, а точки держатся за углы и края: они переживают поворот и
  обрезку, и находят кадр там, где хеш уже сдался.

⚠ **Расстояния хешей — не гарантия.** Они описывают поиск по хешам, а не
современный поиск по картинке: тот считает глубокие признаки сцены и узнаёт кадр
после кропа, перекраски и пересжатия. Ноль здесь означает «точно найдётся»,
большое число — «по хешу не найдётся», и только.

Порог: расстояние **10 из 64** — обычная граница «другое изображение» в
дедупликации; ниже 5 — тот же кадр с правками.

CLI:

    python -m image_.image_unique <исходник> <обработанный>
    python -m image_.image_unique <файл>          # только хеши кадра
"""

from pathlib import Path

# Сторона миниатюры, по которой считают хеш. 8×8 = 64 бита — размер, на котором
# держится вся шкала расстояний ниже.
IMAGE_UNIQUE_SIDE = 8

# Для pHash берём DCT от кадра вчетверо большего и оставляем низкие частоты:
# они несут форму сцены, а высокие — шум и следы JPEG, от которых хеш должен
# быть свободен.
IMAGE_UNIQUE_DCT = 32

# Граница «другое изображение» в битах из 64. Не выдумана: столько берут в
# дедупликации, и на наших кадрах ниже неё оказываются варианты одного кадра.
IMAGE_UNIQUE_FAR = 10

# Сколько точек-примет искать. Больше тысячи — время растёт, а доля совпавших
# уже не меняется.
IMAGE_UNIQUE_POINTS = 1000

# Порог совпадения дескрипторов по Хэммингу (ORB — бинарный, 256 бит).
IMAGE_UNIQUE_MATCH = 64


def image_unique_hash(path) -> dict:
    """Три хеша кадра: `{'ahash','dhash','phash'}` — по 16 шестнадцатеричных цифр."""
    import cv2
    import numpy as np
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f'не прочитан кадр: {path}')
    n = IMAGE_UNIQUE_SIDE
    small = cv2.resize(img, (n, n), interpolation=cv2.INTER_AREA)
    a = small > small.mean()

    # dHash смотрит на ЗНАК перепада между соседями, а не на сами значения:
    # поэтому он не замечает общего осветления кадра, но ловит перерисовку.
    wide = cv2.resize(img, (n + 1, n), interpolation=cv2.INTER_AREA)
    d = wide[:, 1:] > wide[:, :-1]

    big = cv2.resize(img, (IMAGE_UNIQUE_DCT, IMAGE_UNIQUE_DCT),
                     interpolation=cv2.INTER_AREA).astype('float32')
    freq = cv2.dct(big)[:n, :n]
    # Медиана без самого первого коэффициента: в нём общая яркость кадра, и
    # порог по ней сместился бы от любого осветления.
    flat = freq.flatten()[1:]
    p = freq > np.median(flat)

    return {'ahash': _bits_hex(a), 'dhash': _bits_hex(d), 'phash': _bits_hex(p)}


def image_unique_diff(before, after) -> dict:
    """Насколько `after` отличим от `before`: расстояния хешей и доля точек.

    Возвращает `{'ahash','dhash','phash','mean_of','points','matched',
    'points_share','recognizable','threshold'}`. `recognizable` — вердикт по
    хешам: средний сдвиг меньше порога, то есть дедупликация сочтёт кадры одним.
    """
    ha, hb = image_unique_hash(before), image_unique_hash(after)
    bits = {k: _hamming(ha[k], hb[k]) for k in ('ahash', 'dhash', 'phash')}
    mid = round(sum(bits.values()) / 3, 1)
    pts = _points(before, after)
    return {**bits, 'mean_of': mid, **pts,
            'recognizable': mid < IMAGE_UNIQUE_FAR,
            'threshold': IMAGE_UNIQUE_FAR}


def image_unique_cost(before, after) -> dict:
    """Цена обработки: что она сделала с фотореалистичностью кадра.

    Отличие само по себе ничего не стоит — его довели бы до предела одним
    шумом. Поэтому рядом с ним всегда идут три числа, по которым видно, во что
    оно обошлось: зерно (фактура кожи и матрицы), резкость и общая яркость.
    Коридоры те же, что в приёмке подмены лица: зерно 0.8–1.2 от исходного,
    резкость не ниже 0.8 — иначе кадр «глаже оригинала», а это первое, что
    выдаёт обработку.
    """
    import cv2
    import numpy as np
    a = cv2.imread(str(before))
    b = cv2.imread(str(after))
    if a is None or b is None:
        raise ValueError('не прочитан один из кадров')
    # Сравниваем ОДНИ И ТЕ ЖЕ пиксели, а не два кадра целиком. Кроп — законная
    # часть обработки, но он выбрасывает края, где как раз стоят резкие детали
    # фона; сравнив обрезанный кадр с полным, мы получили бы «резкость ×0.61» на
    # пустом месте — не от обработки, а от того, что мерили разные области.
    #
    # Поэтому обрезанный кадр сначала находится в исходном (`matchTemplate`), и
    # дальше сравнивается только его область.
    a, b = _align(a, b)
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    sa, sb = _sigma(ga), _sigma(gb)
    ra = float(cv2.Laplacian(ga, cv2.CV_64F).var())
    rb = float(cv2.Laplacian(gb, cv2.CV_64F).var())
    return {'grain_before': round(sa, 2), 'grain_after': round(sb, 2),
            'grain': round(sb / sa, 2) if sa else 0.0,
            'sharpness_before': round(ra, 1), 'sharpness_after': round(rb, 1),
            'sharpness': round(rb / ra, 2) if ra else 0.0,
            'brightness_before': round(float(np.median(ga)), 1),
            'brightness_after': round(float(np.median(gb)), 1),
            'photoreal': bool(0.8 <= (sb / sa if sa else 0) <= 1.4
                                    and (rb / ra if ra else 0) >= 0.8)}


def _bits_hex(bits) -> str:
    """Матрицу True/False — в шестнадцатеричную строку, по биту на клетку."""
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bool(bit))
    return f'{value:016x}'


def _hamming(a: str, b: str) -> int:
    """Сколько битов разошлось в двух хешах."""
    return bin(int(a, 16) ^ int(b, 16)).count('1')


def _align(a, b):
    """Свести пару к общей области: где в исходном лежит обработанный кадр.

    Обработка вправе обрезать и масштабировать — от этого меняется размер, но
    сравнивать фактуру надо на тех же пикселях. Обрезанный кадр ищется в
    исходном по образцу; масштаб возвращается к исходному шагу, иначе резкость
    мерила бы интерполяцию.

    Не нашлось (кадр перерисован целиком, отражён, повёрнут) — сравниваем как
    есть, приведя к общему размеру: это огрубление, но лучше отказа.
    """
    import cv2
    if a.shape == b.shape:
        return a, b
    ah, aw = a.shape[:2]
    bh, bw = b.shape[:2]
    if bh <= ah and bw <= aw:
        # Ищем по уменьшенным копиям: полноразмерный поиск по кадру 2400×3600
        # считается секунды, а положение рамки нужно с точностью до пикселей,
        # которых всё равно нет у кропа в процентах.
        scale = 640 / max(ah, aw)
        sa = cv2.resize(a, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        sb = cv2.resize(b, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if sb.shape[0] < sa.shape[0] and sb.shape[1] < sa.shape[1]:
            res = cv2.matchTemplate(sa, sb, cv2.TM_CCOEFF_NORMED)
            _, quality, _, loc = cv2.minMaxLoc(res)
            if quality > 0.5:
                x, y = int(loc[0] / scale), int(loc[1] / scale)
                return a[y:y + bh, x:x + bw], b
    if bh > ah or bw > aw:
        b = cv2.resize(b, (aw, ah), interpolation=cv2.INTER_AREA)
        return a, b
    return a, cv2.resize(b, (aw, ah), interpolation=cv2.INTER_AREA)


def _sigma(gray) -> float:
    """Сигма шума по Иммеркеру — та же оценка, что у зерна заплатки.

    Свёртка с ядром, которое гасит любой плавный переход: остаётся только то,
    что меняется от пикселя к пикселю, то есть шум.
    """
    import cv2
    import numpy as np
    k = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype='float32')
    conv = cv2.filter2D(gray.astype('float32'), -1, k)
    return float(np.abs(conv).mean() * np.sqrt(np.pi / 2) / 6)


def _points(before, after) -> dict:
    """Доля точек-примет, совпавших у двух кадров.

    Хеш слепнет от кропа: сдвинули рамку — и биты поехали все сразу, хотя кадр
    тот же. Точки держатся за углы и края, поэтому переживают и обрезку, и
    поворот. Малое число здесь — единственный признак, что кадр не найдут и по
    фрагменту.
    """
    import cv2
    a = cv2.imread(str(before), cv2.IMREAD_GRAYSCALE)
    b = cv2.imread(str(after), cv2.IMREAD_GRAYSCALE)
    orb = cv2.ORB_create(nfeatures=IMAGE_UNIQUE_POINTS)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or not len(ka) or not len(kb):
        return {'points': 0, 'matched': 0, 'points_share': 0.0}
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    good = [m for m in pairs if m.distance <= IMAGE_UNIQUE_MATCH]
    base = min(len(ka), len(kb))
    return {'points': base, 'matched': len(good),
            'points_share': round(len(good) / base, 3) if base else 0.0}


if __name__ == '__main__':
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('before', help='исходный кадр')
    parser.add_argument('after', nargs='?', default='', help='обработанный кадр')
    ns = parser.parse_args()
    if not ns.after:
        print(json.dumps(image_unique_hash(ns.before), ensure_ascii=False, indent=1))
    else:
        out = {**image_unique_diff(ns.before, ns.after),
               'cost': image_unique_cost(ns.before, ns.after)}
        print(json.dumps(out, ensure_ascii=False, indent=1))

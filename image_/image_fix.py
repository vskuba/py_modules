"""
Правка кадров-скриншотов: стирание запечённого текста, выделение штриха в альфу.

Две операции, которые нужны всякий раз, когда скриншот становится подложкой
клона: текст на подложке менять нельзя — его надо стереть и наложить живой
(`image_fix_erase`), а рукописную подпись со светлого фона надо вырвать в
PNG с альфой, чтобы CSS клал её поверх живой подложки (`image_fix_strokes`).

Стирание — не «закрасить средним»: фон страниц и карточек имеет медленный
градиент, плоская заплата на широком окне видна краем. Строка окна
заполняется линейной интерполяцией между медианами узких краёвых полос слева
и справа (или сверху/снизу при `axis='v'`): медиана не тянется в тёмное
опечатками соседнего текста внутри полосы, интерполяция продолжает градиент.

Замеры окон — по правилам `image_scan`: окно обязано оставлять снаружи
полосу чистого фона шириной хотя бы `edge`, иначе интерполировать нечего.
"""
import argparse
import os

import numpy as np
from PIL import Image

# Ширина краевой полосы, из которой берётся медиана: 4-8 px достаточно для
# тона фона и уже достаточно узко, чтобы не захватить соседний текст.
IMAGE_FIX_EDGE = 6

# Качество пересохранения lossy-форматов: webp q92 неотличим от исходника
# скриншота интерфейса и не сыплет артефактами на границе заплатки.
IMAGE_FIX_QUALITY = 92

# Порог и размах альфы для штриха: alpha = (hi − lum) * 255 / span. Светлее
# hi — прозрачно; span пикселей ярности штриха до чёрного — весь диапазон
# альфы (для подписи шариковой ручкой на белой бумаге ~150/120).
IMAGE_FIX_STROKE_HI = 150
IMAGE_FIX_STROKE_SPAN = 120


def image_fix_erase(path, out, boxes, edge=IMAGE_FIX_EDGE, axis='h',
                    quality=IMAGE_FIX_QUALITY) -> dict:
    """
    Стереть прямоугольники, заполнив их продолжением фона.

    Args:
        path: картинка-источник.
        out: куда сохранить (формат по расширению; для webp/jpeg — quality).
        boxes: [(x0, y0, x1, y1)] — окна в конвенции PIL (x1, y1 исключая).
        edge: ширина краевой полосы-донора, px.
        axis: 'h' — интерполяция по строкам от лев/прав полос, 'v' — по
            колонкам от верх/ниж. Выбирают по направлению собственного
            градиента фона и форме окна (высокое узкое окно текста — 'v').
        quality: качество lossy-форматов.

    Returns:
        {'file', 'out', 'boxes': сколько окон стёрто}.

    Raises:
        ValueError: у окна нет ни одной краевой полосы (занимает кадр целиком)
            — продолжения фона не существует, стирать нечем.
    """
    img = Image.open(path)
    mode = img.mode
    arr = np.asarray(img.convert('RGB'), dtype=np.int32)
    h, w = arr.shape[:2]
    for box in boxes:
        x0, y0 = max(int(box[0]), 0), max(int(box[1]), 0)
        x1, y1 = min(int(box[2]), w), min(int(box[3]), h)
        if x0 >= x1 or y0 >= y1:
            continue
        if axis == 'h':
            left = arr[y0:y1, max(x0 - edge, 0):x0]
            right = arr[y0:y1, x1:min(x1 + edge, w)]
            med_axis = 1   # медиана по ширине полосы — своё число на строку
            # медиана отвечает за уровень в середине полосы, поэтому линия
            # идёт «центр левой — центр правой»: на краю окна получается
            # значение фона, а не сдвинутое на полполосы
            lo = (max(x0 - edge, 0) + x0 - 1) / 2.0
            hi = (x1 + min(x1 + edge, w) - 1) / 2.0
            u = np.arange(x0, x1, dtype=np.float64)
            u = ((u - lo) / (hi - lo) if hi > lo else np.zeros_like(u)
                 ).reshape(1, x1 - x0, 1)
        else:
            left = arr[max(y0 - edge, 0):y0, x0:x1]
            right = arr[y1:min(y1 + edge, h), x0:x1]
            med_axis = 0   # по высоте полосы — своё число на колонку
            lo = (max(y0 - edge, 0) + y0 - 1) / 2.0
            hi = (y1 + min(y1 + edge, h) - 1) / 2.0
            u = np.arange(y0, y1, dtype=np.float64)
            u = ((u - lo) / (hi - lo) if hi > lo else np.zeros_like(u)
                 ).reshape(y1 - y0, 1, 1)
        l_a = np.median(left, axis=med_axis, keepdims=True) if left.size else None
        r_a = np.median(right, axis=med_axis, keepdims=True) if right.size else None
        if l_a is None and r_a is None:
            raise ValueError(f'окно {tuple(box)} занимает кадр целиком — '
                             f'краев фона нет, интерполировать нечем')
        if l_a is None or r_a is None:
            # донор только с одной стороны (окно у края кадра): продолжение
            # фона — его уровень; интерполялировать не между чем
            arr[y0:y1, x0:x1] = np.round(
                l_a if l_a is not None else r_a).astype(np.int32)
            continue
        arr[y0:y1, x0:x1] = np.round(l_a + (r_a - l_a) * u).astype(np.int32)
    result = Image.fromarray(arr.astype('uint8'), 'RGB')
    if mode == 'RGBA':
        # скриншот с альфой: стирание живёт в RGB, прозрачность остаётся
        # прежней — вызывающий вправе стирать на полупрозрачном PNG
        alpha = np.asarray(img)[:, :, 3]
        result = Image.merge('RGBA', (*result.split(),
                                      Image.fromarray(alpha, 'L')))
    result.save(out, **_save_kwargs(out, quality))
    return {'file': path, 'out': out, 'boxes': len(boxes)}


def image_fix_strokes(path, out, box, hi=IMAGE_FIX_STROKE_HI,
                      span=IMAGE_FIX_STROKE_SPAN, color=(0, 0, 0)) -> dict:
    """
    Тёмный штрих со светлого фона — в RGBA: альфа линейна по яркости штриха.

    Фото подписи или штампа на светлой бумаге: RGB((hi − lum)·255/span) идёт в
    альфу, сам штрих красится в `color`. Антиалиасинг края превращается в
    плавную альфу автоматически — та же формула, что при ручной ключевке.

    Args:
        path: картинка.
        out: PNG с альфой.
        box: (x0, y0, x1, y1) — окно штриха.
        hi: яркость, светлее которой — прозрачно.
        span: размах яркости штриха до полностью непрозрачного.
        color: цвет штриха в результате.

    Returns:
        {'file', 'out', 'box', 'alpha_max': int}.
    """
    img = np.asarray(Image.open(path).convert('RGB'), dtype=np.int32)
    x0, y0, x1, y1 = (int(v) for v in box)
    reg = img[y0:y1, x0:x1]
    alpha = np.clip((hi - reg.mean(axis=2)) * 255 / span, 0, 255)
    alpha = np.round(alpha).astype('uint8')
    rgb = np.zeros((*alpha.shape, 3), dtype='uint8')
    for i, v in enumerate(color):
        rgb[:, :, i] = v
    out_img = Image.merge('RGBA', (*Image.fromarray(rgb, 'RGB').split(),
                                   Image.fromarray(alpha, 'L')))
    out_img.save(out)
    return {'file': path, 'out': out, 'box': tuple(int(v) for v in box),
            'alpha_max': int(alpha.max()) if alpha.size else 0}


def _save_kwargs(out, quality):
    ext = os.path.splitext(out)[1].lower()
    return {'quality': quality} if ext in {'.jpg', '.jpeg', '.webp'} else {}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Правка кадров-скриншотов')
    ap.add_argument('command', choices=['erase', 'strokes'])
    ap.add_argument('path', help='картинка')
    ap.add_argument('--out', required=True, help='куда сохранить')
    ap.add_argument('--box', action='append', metavar='x0,y0,x1,y1',
                    help='окно, повторять по числу заплаток; strokes — одно')
    ap.add_argument('--edge', type=int, default=IMAGE_FIX_EDGE, help='erase')
    ap.add_argument('--axis', default='h', choices=['h', 'v'], help='erase')
    ap.add_argument('--quality', type=int, default=IMAGE_FIX_QUALITY)
    ap.add_argument('--hi', type=int, default=IMAGE_FIX_STROKE_HI, help='strokes')
    ap.add_argument('--span', type=int, default=IMAGE_FIX_STROKE_SPAN,
                    help='strokes')
    ns = ap.parse_args()
    try:
        if not ns.box:
            raise SystemExit('нужен хотя бы один --box x0,y0,x1,y1')
        boxes = [tuple(int(v) for v in b.split(',')) for b in ns.box]
        if ns.command == 'erase':
            r = image_fix_erase(ns.path, ns.out, boxes, edge=ns.edge,
                                axis=ns.axis, quality=ns.quality)
            print(f"стёрто {r['boxes']} окон → {r['out']}")
        else:
            r = image_fix_strokes(ns.path, ns.out, boxes[0], hi=ns.hi,
                                  span=ns.span)
            print(f"штрих {r['box']} → {r['out']} (альфа до {r['alpha_max']})")
    except (ValueError, FileNotFoundError) as err:
        raise SystemExit(f'ошибка: {err}')

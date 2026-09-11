"""
Стеклянная плитка: прозрачный webp с полупрозрачной пластиной и кромкой.

Для клонов интерфейсов, где поверх живого фона лежит стекло: карточка
полупрозрачна и её цвет следует обоям, — непрозрачная запечь-картинка так не
умеет и на тёмной фазе выступает пятном. Печатаем RGBA: вне скругления полная
прозрачность, внутри — заливка цвета F с альфой a, по контуру — необязательная
тёмная кромка (rim), маска «внешний контур минус внутренний». Поверх
подложки, если задан источник (`ink`), кладём глифы и текст из дампа, маскируя
по яркости: фон карточки (медиана яркости поля) даёт ровно ноль, почернение
сверх offset растёт до полной непрозрачности — иначе светлая полоска дампа
тянет тон половины карточки.

Заливку выбирают по измеренной дельте оригинала стеклянным контрактом
`delta = a * (F - bg)` — `image_tone_glass_solve`; проверяют клон живым
снимком — `image_tone_glass_verify` (`image_tone.md`).

Грабли, зашитые в код:
- потерочный WebP на малой альфе даёт тёмную бахрому по скруглению —
  сохраняем lossless=True, exact=True;
- RGB под альфой=0 красим цветом заливки, а не оставляём чёрным — страховка
  от premultiply-истолкования;
- Pillow рисует скругление жёстко, живой же край мягкий (4–5 px): маску
  рисуем в SS-кратном суперсэмплинге и ужимаем BOX;
- композитинг — модульный `Image.alpha_composite(base, over)`: у метода
  `.alpha_composite` возврата нет, он портит базу на месте; и после него
  RGB под полной прозрачностью нужно подкрасить заново — композит гасит его
  в ноль, что и делает `_fill_under_alpha`.
"""
import os

import numpy as np
from PIL import Image, ImageDraw

IMAGE_GLASS_SS = 4            # суперсэмплинг маски: край после ужимки мягкий
IMAGE_GLASS_INK_OFFSET = 10   # почернение от фона карточки, с которого у глифов появляется альфа
IMAGE_GLASS_INK_SLOPE = 30.0  # а столько темнее фона глиф полностью непрозрачен


def image_glass_rounded_mask(size, rects, radius, ss=IMAGE_GLASS_SS) -> np.ndarray:
    """
    Маска скруглённых прямоугольников, float 0..1; на краях — антиалиасинг.

    Рисуем в ss-кратном разрешении и ужимаем BOX: Pillow рисует радиус
    жёсткой дугой, живой же край стекла мягкий, и жёсткая маска на
    полупрозрачной пластине читается лесенкой по скруглению.

    Args:
        size: (w, h) холста.
        rects: [(x0, y0, x1, y1), …] — плиты в пикселях холста.
        radius: скругление углов, px.
        ss: суперсэмплинг маски, `IMAGE_GLASS_SS`.

    Returns:
        np.ndarray (h, w) float 0..1; вне плит — нули.

    Raises:
        ValueError: rect не лежит в холсте.
    """
    w, h = (int(v) for v in size)
    big = Image.new('L', (w * ss, h * ss), 0)
    draw = ImageDraw.Draw(big)
    r = int(radius) * ss
    for rect in rects:
        x0, y0, x1, y1 = (int(v) for v in rect)
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError(f'rect {tuple(rect)} не лежит в холсте {w}x{h}')
        draw.rounded_rectangle([x0 * ss, y0 * ss, x1 * ss, y1 * ss], radius=r, fill=255)
    return np.asarray(big.resize((w, h), Image.BOX), dtype=np.float64) / 255.0


def image_glass_rim_mask(size, rects, radius, rim_w=2, ss=IMAGE_GLASS_SS) -> np.ndarray:
    """
    Маска контура шириной `rim_w`: внешняя маска минус внутренняя.

    Внутренний прямоугольник сдвинут на rim_w к центру и имеет радиус
    radius - rim_w (арифметика скруглённого отступа); где радиус меньше
    удвоенной ширины кромки — внутренний берёт нулевой радиус, иначе дуга
    съела бы кромку на углах.
    """
    outer = image_glass_rounded_mask(size, rects, radius, ss=ss)
    inner_rects = [(x0 + rim_w, y0 + rim_w, x1 - rim_w, y1 - rim_w)
                   for x0, y0, x1, y1 in rects]
    inner = image_glass_rounded_mask(size, inner_rects, max(radius - rim_w, 0), ss=ss)
    return np.clip(outer - inner, 0, 1)


def image_glass_plate(size, rects, fill, radius=0, rim=None, rim_w=2,
                      ss=IMAGE_GLASS_SS) -> Image.Image:
    """
    Стеклянная подложка RGBA: полупрозрачные плиты с кромкой, фон прозрачен.

    Args:
        size: (w, h) холста.
        rects: [(x0, y0, x1, y1), …] — плиты.
        fill: (r, g, b, a) — цвет и альфа пластины 0..255; без альфы — 255.
        radius: скругление углов, px.
        rim: (r, g, b, a) — кромка; None или нулевая альфа — без кромки.
        rim_w: ширина кромки, px.
        ss: суперсэмплинг масок.

    Returns:
        PIL RGBA; под полной прозрачностью RGB — цвет заливки (страховка от
        premultiply), альфа вне плит — ровно 0.

    Raises:
        ValueError: fill без каналов или alpha пластины вне 0..255.
    """
    f = _color(fill, 'fill')
    if not 0 <= f[3] <= 255:
        raise ValueError(f'альфа fill вне 0..255: {fill!r}')
    w, h = (int(v) for v in size)
    mask = image_glass_rounded_mask(size, rects, radius, ss=ss)
    base = np.zeros((h, w, 4), dtype=np.uint8)
    base[:, :, :3] = f[:3]                       # под альфой — цвет заливки, не чёрный
    base[:, :, 3] = (mask * f[3]).astype(np.uint8)
    out = Image.fromarray(base, 'RGBA')
    if rim is not None:
        r = _color(rim, 'rim')
        if r[3] > 0 and rim_w > 0:
            rim_layer = np.zeros((h, w, 4), dtype=np.uint8)
            rim_layer[:, :, :3] = r[:3]
            rim_m = image_glass_rim_mask(size, rects, radius, rim_w=rim_w, ss=ss)
            rim_layer[:, :, 3] = (rim_m * r[3]).astype(np.uint8)
            out = Image.alpha_composite(out, Image.fromarray(rim_layer, 'RGBA'))
    return _fill_under_alpha(out, f)


def image_glass_bake(src, out, rects, fill, radius=0, rim=None, rim_w=2, ink=True,
                     ink_offset=IMAGE_GLASS_INK_OFFSET, ink_slope=IMAGE_GLASS_INK_SLOPE,
                     crop=None, ss=IMAGE_GLASS_SS) -> dict:
    """
    Запечь плитку: подложка + (опционально) глифы из дампа, — и сохранить webp.

    Источник даёт размер холста, крой и (при ink=True) слой глифов: внутри
    каждой плиты пиксели темнее её фона на `ink_offset` получают альфу,
    доходящую до полной к `ink_offset + ink_slope` ниже медианы яркости поля
    плиты; фон карточки даёт ровно ноль и тон пластины не портит. Слои
    склеиваются `Image.alpha_composite`, сохраняется всегда lossless+exact
    (см. грабли в докстринге модуля).

    Args:
        src: путь к изображению-источнику (дамп экрана, кадр нужной области).
        out: файл для записи webp.
        rects, fill, radius, rim, rim_w, ss: как в `image_glass_plate`.
        ink: False — только подложка, глифы не поднимаем.
        ink_offset, ink_slope: маска глифов по яркости.
        crop: (x0, y0, x1, y1) — крой источника перед печатью (например,
            снять статус-бар дампа).
        ss: суперсэмплинг масок.

    Returns:
        {'out', 'size', 'bytes', 'alpha_share' — доля пикселей с альфой > 0,
         'rects'}; доля — контроль, что плита вообще на холсте.

    Raises:
        ValueError: rect не лежит в холсте; fill/rim не цвета.
    """
    img = Image.open(src)
    if crop is not None:
        img = img.crop(tuple(int(v) for v in crop))
    arr = np.asarray(img.convert('RGB'), dtype=np.float64)
    plate = image_glass_plate(img.size, rects, fill, radius=radius, rim=rim,
                              rim_w=rim_w, ss=ss)
    canvas = plate
    if ink:
        mask = image_glass_rounded_mask(img.size, rects, radius, ss=ss)
        layer = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
        for x0, y0, x1, y1 in rects:
            x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
            crop_arr = arr[y0:y1, x0:x1]
            lum = crop_arr.mean(axis=2)
            field = float(np.median(lum))        # фон поля: мода яркости внутри плиты
            m = np.clip((field - lum - ink_offset) / float(ink_slope), 0, 1)
            m *= mask[y0:y1, x0:x1]              # глифы не выходят за скругление
            blk = layer[y0:y1, x0:x1]
            blk[:, :, :3] = crop_arr.astype(np.uint8)
            blk[:, :, 3] = (m * 255).astype(np.uint8)
        canvas = Image.alpha_composite(canvas, Image.fromarray(layer, 'RGBA'))
    canvas = _fill_under_alpha(canvas, _color(fill, 'fill'))
    canvas.save(out, 'WEBP', lossless=True, exact=True)
    a = np.asarray(canvas.getchannel('A'), dtype=np.int64)
    return {'out': out, 'size': img.size, 'bytes': os.path.getsize(out),
            'alpha_share': round(float((a > 0).mean()), 4), 'rects': list(rects)}


def _fill_under_alpha(img: Image.Image, fill) -> Image.Image:
    """Под полной прозрачностью — цвет заливки, не чёрный: страховка от premultiply."""
    a = np.array(img.convert('RGBA'))  # не asarray: buffer PIL read-only, правка требует копию
    a[a[:, :, 3] == 0, :3] = _color(fill, 'fill')[:3]
    return Image.fromarray(a, 'RGBA')


def _color(value, name) -> tuple:
    """Цвет (r, g, b, a) из тройки или четвёрки; без альфы — 255."""
    parts = tuple(int(v) for v in value)
    if len(parts) not in (3, 4):
        raise ValueError(f'{name} — (r, g, b) или (r, g, b, a), получено {value!r}')
    return parts if len(parts) == 4 else parts + (255,)


def _rects(raw) -> list:
    """Список плит из строки 'x0,y0,x1,y1;x0,y0,x1,y1' или из списка кортежей."""
    if isinstance(raw, str):
        raw = [tuple(int(v) for v in part.split(',')) for part in raw.split(';') if part.strip()]
    return [tuple(int(v) for v in rect) for rect in raw]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description='Стеклянная плитка: прозрачный webp с полупрозрачной '
                    'пластиной, кромкой и глифами из дампа.')
    ap.add_argument('src', help='изображение-источник (дамп экрана)')
    ap.add_argument('out', help='файл для записи webp')
    ap.add_argument('--rects', required=True, help='плиты: x0,y0,x1,y1 через ;')
    ap.add_argument('--fill', required=True, help='заливка r,g,b,a (альфа 0..255)')
    ap.add_argument('--radius', type=int, default=0, help='скругление углов, px')
    ap.add_argument('--rim', default='', help='кромка r,g,b,a; пусто — без кромки')
    ap.add_argument('--rim-w', type=int, default=2, help='ширина кромки, px')
    ap.add_argument('--no-ink', action='store_true', help='глифы не поднимать, только подложка')
    ap.add_argument('--ink-offset', type=float, default=IMAGE_GLASS_INK_OFFSET,
                    help='почернение от фона карточки, с которого появляется альфа')
    ap.add_argument('--ink-slope', type=float, default=IMAGE_GLASS_INK_SLOPE,
                    help='ещё столько же темнее — глиф непрозрачен')
    ap.add_argument('--crop', default='', help='крой источника x0,y0,x1,y1')
    ap.add_argument('--ss', type=int, default=IMAGE_GLASS_SS, help='суперсэмплинг масок')
    ns = ap.parse_args()

    try:
        res = image_glass_bake(ns.src, ns.out, _rects(ns.rects),
                               tuple(int(v) for v in ns.fill.split(',')),
                               radius=ns.radius,
                               rim=tuple(int(v) for v in ns.rim.split(',')) if ns.rim else None,
                               rim_w=ns.rim_w, ink=not ns.no_ink,
                               ink_offset=ns.ink_offset, ink_slope=ns.ink_slope,
                               crop=tuple(int(v) for v in ns.crop.split(',')) if ns.crop else None,
                               ss=ns.ss)
        print(f'{res["out"]}: {res["size"][0]}×{res["size"][1]}, доля alpha>0 '
              f'{res["alpha_share"]:.3f}, {res["bytes"]} Б')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')


if __name__ == '__main__':
    main()

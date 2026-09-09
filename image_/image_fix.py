"""
Правка кадров-скриншотов: стирание запечённого текста, двумерная зачистка
со стеклом (inpaint), плоская заливка окна, выделение штриха в альфу.

Операции, которые нужны всякий раз, когда скриншот становится подложкой
клона: текст на подложке менять нельзя — его надо стереть и наложить живой
(`image_fix_erase`), окно слота с запечённым фото — залить цветом подложки,
когда он замерен (`image_fix_fill`), а рукописную подпись со светлого фона
надо вырвать в PNG с альфой, чтобы CSS клал её поверх живой подложки
(`image_fix_strokes`).

Стирание — не «закрасить средним»: фон страниц и карточек имеет медленный
градиент, плоская заплата на широком окне видна краем. Строка окна
заполняется линейной интерполяцией между медианами узких краёвых полос слева
и справа (или сверху/снизу при `axis='v'`): медиана не тянется в тёмное
опечатками соседнего текста внутри полосы, интерполяция продолжает градиент.
Когда же тон окна известен точнее доноров (слот цвета подложки, заглушка
поля) — плоская заливка честнее интерполяции, для неё `image_fix_fill`.
Если же фон окна меняется по обеим осям (косой градиент стеклянной карточки),
одномерного продолжения мало — `image_fix_inpaint` продолжает его по всем
четырём сторонам (Coon-патч) и докладывает гауссова зерна: вычищенное окно
не должно выглядеть мутным стеклом на живом шуме подложки.

Альфа — сквозная сквозь любую правку: стирание и заливка живут в RGB,
прозрачность кадра возвращается нетронутой. Потерять альфу молча — почерневшая
матовая подложка на живом CSS-фоне вместо прозрачных областей.

Замеры окон — по правилам `image_scan`: окно обязано оставлять снаружи
полосу чистого фона шириной хотя бы `edge`, иначе интерполировать нечего.
"""
import argparse
import os

import numpy as np
from PIL import Image

from image_.image_layout import image_layout_alpha


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

# Кромка заливки, px: тон заливки непрерывен с окружением (окно залито
# замеренным цветом подложки), фейд нужен только чтобы спрятать ±1 px сдвига
# края lossy-сжатием — большая кромка сама стала бы видимой полосой.
IMAGE_FIX_FILL_FEATHER = 2.0

# Зерно заплатки, σ в каналах 0-255: стекло скриншота не плоское — lossy-
# сжатие и шум матрицы оставляют в ровных зонах живое зерно; плоский патч
# поверх него выглядит мутным стеклом. σ 2.8 измерено по остаточному
# разбросу ровных зон карточек ua.gov.diia.app; 0 — без шума.
IMAGE_FIX_INPAINT_SIGMA = 2.8


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
    # Альфа живёт отдельно от правки и любого режима-источника (RGBA, LA,
    # P с прозрачностью): erase трогает только RGB. LA и P на выходе становятся
    # RGBA — иначе Pillow молча вернёт RGB и прозрачность сгорит в матовую
    # подложку.
    has_alpha = 'A' in img.mode or (img.mode == 'P'
                                    and 'transparency' in img.info)
    alpha = np.asarray(img.convert('RGBA'))[:, :, 3] if has_alpha else None
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
    if alpha is not None:
        result = Image.merge('RGBA', (*result.split(),
                                      Image.fromarray(alpha, 'L')))
    result.save(out, **_save_kwargs(out, quality))
    return {'file': path, 'out': out, 'boxes': len(boxes)}


def image_fix_inpaint(path, out, boxes, edge=IMAGE_FIX_EDGE,
                      sigma=IMAGE_FIX_INPAINT_SIGMA,
                      quality=IMAGE_FIX_QUALITY) -> dict:
    """
    Стереть прямоугольники двумерным продолжением фона (Coon-патч) с зерном.

    Когда erase с одним направлением несправедлив: фон окна меняется по обеим
    осям (стеклянные карточки с косым градиентом, тикерные полосы), и заплата,
    интерполированная вдоль одной оси, тянет градиент гребнем. Здесь каждая
    из четырёх сторон окна даёт свою скалярную медиану из прилегающей донорской
    полосы шириной `edge` (скаляр на канал: np.median по axis=-1 на RGBA свалил
    бы каналы в одно число, потому reshape в (-1, ch)), углы — из диагональных
    квадратиков, нутро — билинейный Coon (угловые слагаемые снимают скачок там,
    где стороны набегают друг на друга). Шум — гауссов σ по RGB-каналам, альфу
    не трогает никогда: стекло не плоское, заплатка без зерна выглядит мутным
    стеклом. Донор стороны отсутствует (окно у края кадра) — сторона берётся
    средним доступных; нет ни одной стороны — ValueError. Зерно детерминировано
    (seed 0): та же картинка и окна дают тот же байт за байтом выход.
    """
    img = Image.open(path)
    has_alpha = 'A' in img.mode or (img.mode == 'P'
                                    and 'transparency' in img.info)
    alpha = np.asarray(img.convert('RGBA'))[:, :, 3] if has_alpha else None
    arr = np.asarray(img.convert('RGB'), dtype=np.float64)
    h, w = arr.shape[:2]
    rng = np.random.default_rng(0)
    for box in boxes:
        x0, y0 = max(int(box[0]), 0), max(int(box[1]), 0)
        x1, y1 = min(int(box[2]), w), min(int(box[3]), h)
        if x0 >= x1 or y0 >= y1:
            continue
        sides = [arr[y0:y1, max(x0 - edge, 0):x0],      # левая
                 arr[y0:y1, x1:min(x1 + edge, w)],      # правая
                 arr[max(y0 - edge, 0):y0, x0:x1],      # верхняя
                 arr[y1:min(y1 + edge, h), x0:x1]]      # нижняя
        vals = [_med3(s) if s.size else None for s in sides]
        got = [v for v in vals if v is not None]
        if not got:
            raise ValueError(f'окно {tuple(box)} занимает кадр целиком — '
                             f'краев фона нет, продолжать нечем')
        base = np.mean(got, axis=0)
        l, r, t, b = (v if v is not None else base for v in vals)

        def _corner(region, a, c):
            # угол — из диагонального квадратика; у края кадра его нет,
            # берём средним двух прилежащих сторон (уже с подстановкой)
            reg = np.asarray(region)
            return _med3(reg) if reg.size else (a + c) / 2.0

        c_tl = _corner(arr[max(y0 - edge, 0):y0, max(x0 - edge, 0):x0], l, t)
        c_tr = _corner(arr[max(y0 - edge, 0):y0, x1:min(x1 + edge, w)], r, t)
        c_bl = _corner(arr[y1:min(y1 + edge, h), max(x0 - edge, 0):x0], l, b)
        c_br = _corner(arr[y1:min(y1 + edge, h), x1:min(x1 + edge, w)], r, b)
        bh, bw = y1 - y0, x1 - x0
        # середины пикселей в 0..1: уровень стороны отвечает за центр донорской
        # полосы, но стороны здесь скаляры и продолжение фона — линейное,
        # сдвиг на полпикселя на широких окнах заметнее экономии точности
        u = ((np.arange(bw) + 0.5) / bw).reshape(1, bw, 1)
        v = ((np.arange(bh) + 0.5) / bh).reshape(bh, 1, 1)
        patch = ((1 - u) * l + u * r + (1 - v) * t + v * b
                 - ((1 - u) * (1 - v) * c_tl + (1 - u) * v * c_bl
                    + u * (1 - v) * c_tr + u * v * c_br))
        if sigma > 0:
            patch = patch + rng.normal(0.0, sigma, patch.shape)
        arr[y0:y1, x0:x1] = patch
    result = Image.fromarray(
        np.clip(np.round(arr), 0, 255).astype('uint8'), 'RGB')
    if alpha is not None:
        result = Image.merge('RGBA', (*result.split(),
                                      Image.fromarray(alpha, 'L')))
    result.save(out, **_save_kwargs(out, quality))
    return {'file': path, 'out': out, 'boxes': len(boxes), 'sigma': sigma}


def image_fix_fill(path, out, boxes, color, radius=0,
                   feather=IMAGE_FIX_FILL_FEATHER,
                   quality=IMAGE_FIX_QUALITY) -> dict:
    """
    Залить окна плоским замеренным цветом; скругление — дугой `radius`.

    Между erase и закрашиванием «на глаз» middle ground, когда тон окна
    известен точнее доноров erase: слот фотографии цвета подложки, заглушка
    поля формы. Маска — знаковое расстояние до скруглённого прямоугольника
    (`image_layout_alpha`): без дуги на скруглённом окне плоская заплата
    выдаёт белый ореол по углам наружу и полосу цвета под низом. Альфа
    источника проходит насквозь, как у erase.

    Args:
        path: картинка-источник.
        out: куда сохранить (формат по расширению; для webp/jpeg — quality).
        boxes: [(x0, y0, x1, y1)] — окна в конвенции PIL (x1, y1 исключая).
        color: '#rrggbb' или (r, g, b) 0-255.
        radius: радиус скругления окна, px; больше половины короткой стороны
            не бывает — ограничивается.
        feather: ширина мягкого края маски, px.
        quality: качество lossy-форматов.

    Returns:
        {'file', 'out', 'boxes': сколько окон залито, 'color': (r, g, b)}.

    Raises:
        ValueError: цвет не '#rrggbb' и не тройка чисел.
    """
    img = Image.open(path)
    has_alpha = 'A' in img.mode or (img.mode == 'P'
                                    and 'transparency' in img.info)
    alpha = np.asarray(img.convert('RGBA'))[:, :, 3] if has_alpha else None
    rgb = np.asarray(img.convert('RGB'), dtype=np.float32)
    h, w = rgb.shape[:2]
    col = np.array(_fill_rgb(color), dtype=np.float32)
    for box in boxes:
        x0, y0 = max(int(box[0]), 0), max(int(box[1]), 0)
        x1, y1 = min(int(box[2]), w), min(int(box[3]), h)
        if x0 >= x1 or y0 >= y1:
            continue
        # радиус за пределы короткой стороны уходит в вырожденную маску
        r = min(float(radius), (x1 - x0) / 2.0, (y1 - y0) / 2.0)
        m = image_layout_alpha(h, w, (x0, y0, x1, y1), r,
                               float(feather))[:, :, None] / 255.0
        rgb = rgb * (1 - m) + col * m
    result = Image.fromarray(
        np.clip(np.round(rgb), 0, 255).astype('uint8'), 'RGB')
    if alpha is not None:
        result = Image.merge('RGBA', (*result.split(),
                                      Image.fromarray(alpha, 'L')))
    result.save(out, **_save_kwargs(out, quality))
    return {'file': path, 'out': out, 'boxes': len(boxes),
            'color': tuple(int(v) for v in col)}


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


def _med3(region):
    """Скалярная медиана области по каналам: flatten в (-1, ch) на канал."""
    return np.median(region.reshape(-1, region.shape[-1]), axis=0)


def _fill_rgb(color) -> tuple:
    """Цвет заливки в (r, g, b): '#rrggbb' / '#rgb' или тройка чисел 0-255."""
    if isinstance(color, str):
        t = color.strip().lstrip('#')
        if len(t) == 3:
            t = ''.join(c * 2 for c in t)
        if len(t) == 6:
            try:
                return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
        raise ValueError(f'цвет {color!r}: ждём "#rrggbb" или тройку (r, g, b)')
    try:
        r, g, b = (int(v) for v in color)
    except (TypeError, ValueError):
        raise ValueError(f'цвет {color!r}: ждём "#rrggbb" или тройку (r, g, b)') from None
    if not all(0 <= v <= 255 for v in (r, g, b)):
        raise ValueError(f'цвет {color!r}: каналы вне 0-255')
    return (r, g, b)


def _save_kwargs(out, quality):
    ext = os.path.splitext(out)[1].lower()
    return {'quality': quality} if ext in {'.jpg', '.jpeg', '.webp'} else {}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Правка кадров-скриншотов')
    ap.add_argument('command', choices=['erase', 'inpaint', 'fill', 'strokes'])
    ap.add_argument('path', help='картинка')
    ap.add_argument('--out', required=True, help='куда сохранить')
    ap.add_argument('--box', action='append', metavar='x0,y0,x1,y1',
                    help='окно, повторять по числу заплаток; strokes — одно')
    ap.add_argument('--edge', type=int, default=IMAGE_FIX_EDGE,
                    help='erase/inpaint: ширина донорской полосы, px')
    ap.add_argument('--sigma', type=float, default=IMAGE_FIX_INPAINT_SIGMA,
                    help='inpaint: зерно по RGB, 0 — без шума')
    ap.add_argument('--axis', default='h', choices=['h', 'v'], help='erase')
    ap.add_argument('--color', help='fill: "#rrggbb"')
    ap.add_argument('--radius', type=int, default=0, help='fill: скругление, px')
    ap.add_argument('--feather', type=float, default=IMAGE_FIX_FILL_FEATHER,
                    help='fill: ширина мягкого края маски, px')
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
        elif ns.command == 'inpaint':
            r = image_fix_inpaint(ns.path, ns.out, boxes, edge=ns.edge,
                                  sigma=ns.sigma, quality=ns.quality)
            print(f"зачищено {r['boxes']} окон (зерно σ={r['sigma']}) "
                  f"→ {r['out']}")
        elif ns.command == 'fill':
            if not ns.color:
                raise SystemExit('fill требует --color "#rrggbb"')
            r = image_fix_fill(ns.path, ns.out, boxes, ns.color,
                               radius=ns.radius, feather=ns.feather,
                               quality=ns.quality)
            print(f"залито {r['boxes']} окон "
                  f"{'#%02x%02x%02x' % r['color']} → {r['out']}")
        else:
            r = image_fix_strokes(ns.path, ns.out, boxes[0], hi=ns.hi,
                                  span=ns.span)
            print(f"штрих {r['box']} → {r['out']} (альфа до {r['alpha_max']})")
    except (ValueError, FileNotFoundError) as err:
        raise SystemExit(f'ошибка: {err}')

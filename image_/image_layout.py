"""
Геометрия кадра: где фон, где граница контентного блока, где точки-индикатор,
вырез скруглённого прямоугольника.

Отвечает на вопросы, которые не сводятся к тону и швам: экран наполовину
закрыт карточкой, а знать надо именно фон — «какого цвета обои под контентом»
(`image_layout_bgfield`: поле фона достраивается по тонким боковым полоскам и
медианам крайних зон, там, где сам кадр ничего не показывает). Зная поле,
видно и обратное — «где кончается контент»: граница ищется по самому крутому
перепаду |кадр − поле| (`image_layout_edge`), а не порогом: яркая мелочь
(точки, подписи) раздувает пороговую полосу дальше настоящей границы.
Для каруселей — «сколько точек в строке и какая активна»
(`image_layout_dots`). Для сборки «живой фон + картинка слоем» — альфа-вырез
скруглённого прямоугольника (`image_layout_alpha`, `image_layout_cutout`).

Пороговые числа здесь — умолчания, а не истина: конкретный экран обычно
требует своих окон (замеряются тем же `edge`/`dots` по этому кадру).
Грабли замера — `image_layout.md`.
"""
import argparse
from pathlib import Path

import numpy as np
from PIL import Image

# Боковые полоски, по которым достраивается поле фона: доля ширины кадра.
# 4% — компромисс: уже — попадает в скруглённые углы карточек и тень под
# панелью вкладок, шире — съедает контент на узких экранах.
IMAGE_LAYOUT_LBAND_FRAC = 0.04

# Зоны-якоря вертикальной коррекции поля фона (доли высоты): полосы медиан и
# точка, к которой поле притягивается. Умолчания сняты с цельноэкранного
# кадра мобильного приложения: верхняя якорная полоса под статус-баром,
# нижняя — над панелью вкладок, где контент обычно не бывает.
IMAGE_LAYOUT_TOP_ROWS = (0.03, 0.10)
IMAGE_LAYOUT_TOP_ANCHOR = 0.065
IMAGE_LAYOUT_BOT_ROWS = (0.79, 0.96)
IMAGE_LAYOUT_BOT_ANCHOR = 0.87

# До высоты, ниже/выше которой поле тянется к нижней/верхней зоне; между
# ними работает горизонтальная интерполяция боковых полосок.
IMAGE_LAYOUT_BLEND_TOP = 0.25
IMAGE_LAYOUT_BLEND_BOT = 0.755

# Сглаживание профиля при поиске границы (строк/колонок). По умолчанию выключено:
# скользящее среднее сдвигает границу на окно/2 (проверено: стекло карточки
# уезжало с 325 на 309), а от одиночного шума бережёт медиана поперёк полосы.
# Включать стоит только на полосе, где иначе шум забивает перепад, — и помнить
# про сдвиг.
IMAGE_LAYOUT_EDGE_SMOOTH = 0

# Полоса поперёк оси для профиля по умолчанию: центр кадра, где меньше всего
# фотографий; на реальном экране окно берут над безглифной зоной (грабли).
IMAGE_LAYOUT_EDGE_BAND = (0.35, 0.65)

# Отклонение колонки от поля фона (максимум по полосе), с которого она
# считается точкой-индикатором: на живой карусели inactive точки дают ~10,
# wallpaper — единицы, активная — вдвое больше inactive.
IMAGE_LAYOUT_DOTS_TOL = 10

# Минимальная ширина blob'а точки: меньше — одиночный пиксель шума или
# артефакт JPEG, точка-индикатор уже бывает ~10 px.
IMAGE_LAYOUT_DOTS_MIN_W = 6

# Фейд кромки выреза: JPEG-перекодирование сдвигает край картинки на ±1 px,
# резкая граница на живом фоне тогда читается белёсой ниткой; 8 px перехода
# эту разницу прячут, а на глаз кромка остаётся чёткой.
IMAGE_LAYOUT_CUT_FEATHER = 8.0


def image_layout_bgfield(src, left=None, right=None, top_rows=None,
                         top_anchor=None, bot_rows=None, bot_anchor=None,
                         blend_top=None, blend_bot=None):
    """Поле фона кадра: float32 (h, w, 3) — «какого цвета здесь был бы фон».

    Боковые полоски (по умолчанию по 4% ширины у краёв) не тронуты контентом,
    их медианы по строкам интерполируются горизонтально на всю ширину; сверху
    и снизу поле притягивается к медианам зон-якорей (по умолчанию под
    статус-баром и над панелью вкладок). Так достраивается плавный фон под
    карточкой, занимающей полэкрана: без коррекции горизонтальная интерполяция
    расходится с реальным фоном у верхнего/нижнего края.

    src — путь, PIL-картинка или массив; границы окон — пикселями (числа),
    умолчания — доли кадра.
    """
    a = _to_float(src)
    h, w = a.shape[:2]
    bl = _px(left, (0.0, IMAGE_LAYOUT_LBAND_FRAC), w)
    br = _px(right, (1.0 - IMAGE_LAYOUT_LBAND_FRAC, 1.0), w)
    l = np.median(a[:, bl[0]:bl[1], :], axis=1)  # (h, 3)
    r = np.median(a[:, br[0]:br[1], :], axis=1)  # (h, 3)
    xl = 0.5 * (bl[0] + bl[1] - 1)
    xr = 0.5 * (br[0] + br[1] - 1)
    t = ((np.arange(w, dtype=np.float32) - xl) / (xr - xl))[None, :, None]
    lr = l[:, None, :] * (1 - t) + r[:, None, :] * t
    tr = _px(top_rows, IMAGE_LAYOUT_TOP_ROWS, h)
    ba = _px(bot_rows, IMAGE_LAYOUT_BOT_ROWS, h)
    top = np.median(a[tr[0]:tr[1], :, :], axis=0)
    bot = np.median(a[ba[0]:ba[1], :, :], axis=0)
    y = np.arange(h, dtype=np.float32)[:, None, None]
    b_top = _px1(blend_top, IMAGE_LAYOUT_BLEND_TOP, h)
    b_bot = _px1(blend_bot, IMAGE_LAYOUT_BLEND_BOT, h)
    wt = np.clip((b_top - y) / (b_top - tr[1]), 0, 1)
    wb = np.clip((y - b_bot) / (ba[0] - b_bot), 0, 1)
    anchor_t = _px1(top_anchor, IMAGE_LAYOUT_TOP_ANCHOR, h)
    anchor_b = _px1(bot_anchor, IMAGE_LAYOUT_BOT_ANCHOR, h)
    corr = wt * (top - lr[anchor_t])[None] + wb * (bot - lr[anchor_b])[None]
    return lr + corr


def image_layout_edge(src, ref=None, band=None, axis='y', search=None,
                      smooth=IMAGE_LAYOUT_EDGE_SMOOTH):
    """Граница контентного блока по самому крутому перепаду |кадр − поле|.

    Возвращает rise_at (контент начинается — крутой подъём отклика) и
    fall_at (контент заканчивается — крутой спад) вдоль оси. Порог для этого
    неприменим: bright-мелочь (строка точек, подпись) держит |кадр − поле|
    выше порога и уводит границу за собой, а перепад остаётся на месте
    контента. ref — готовое поле фона (`image_layout_bgfield`), по умолчанию
    считается им же; band — окно поперёк оси (по умолчанию центр 30% кадра,
    на реальном экране его берут над безглифной зоной).
    """
    a = _to_float(src)
    field = _to_float(ref) if ref is not None else image_layout_bgfield(a)
    d = np.abs(a - field).mean(axis=2)
    if axis == 'y':
        x0, x1 = _px(band, IMAGE_LAYOUT_EDGE_BAND, a.shape[1])
        prof = np.median(d[:, x0:x1], axis=1)
    elif axis == 'x':
        y0, y1 = _px(band, IMAGE_LAYOUT_EDGE_BAND, a.shape[0])
        prof = np.median(d[y0:y1, :], axis=0)
    else:
        raise ValueError(f"axis: ждут 'y' или 'x', дали {axis!r}")
    p = _moving_avg(prof, smooth) if smooth > 1 else prof
    g = np.diff(p)
    lo, hi = _px(search, (0.05, 0.95), g.shape[0])
    g = g[lo:hi]
    # Граница — строка нового уровня: g[i] = p[i+1] − p[i], значит переход
    # между i и i+1 принадлежит строке i+1 (проверено на стекле карточки).
    rise = lo + int(np.argmax(g)) + 1
    fall = lo + int(np.argmin(g)) + 1
    return {'axis': axis,
            'rise_at': int(rise), 'rise_step': round(float(g[rise - lo - 1]), 1),
            'fall_at': int(fall), 'fall_step': round(float(g[fall - lo - 1]), 1)}


def image_layout_dots(src, band, ref=None, tol=IMAGE_LAYOUT_DOTS_TOL,
                      min_width=IMAGE_LAYOUT_DOTS_MIN_W):
    """Строка точек-индикаторов: центры, ширины, пики и активный слот.

    band = (y0, y1) — горизонтальная полоса, в которой лежат точки. Для
    каждой колонки считается максимум |кадр − поле| по полосе: точка тонкая
    (20 px в кадре 2400), усреднение по полосе размывает её в wallpaper.
    Колонки выше tol собираются в непрерывные отрезки (разрыв ≤ 3 px не
    разрывает — AA-переходы). Активной считается точка с наибольшим пиком:
    на живой карусели активная вдвое ярче поля, inactive — лишь на несколько
    ступеней выше шума, поэтому вердикт по средней яркости (argmax luma)
    врал, а пик отклонения разводит слоты надёжно.
    """
    a = _to_float(src)
    field = _to_float(ref) if ref is not None else image_layout_bgfield(a)
    y0, y1 = band
    dev = np.abs(a[y0:y1] - field[y0:y1]).mean(axis=2).max(axis=0)
    dots, run = [], None
    for x, hit in enumerate(dev > tol):
        if hit:
            if run is None:
                run = [x, x]
            elif x - run[1] <= 3:
                run[1] = x
            else:
                dots.append(run)
                run = [x, x]
    if run is not None:
        dots.append(run)
    out = [{'center': int((r0 + r1) // 2), 'width': int(r1 - r0 + 1),
            'peak': int(dev[r0:r1 + 1].max())}
           for r0, r1 in dots if r1 - r0 + 1 >= min_width]
    active = max(range(len(out)), key=lambda i: out[i]['peak']) if out else None
    pitch = None
    if len(out) > 1:
        steps = np.diff([dot['center'] for dot in out])
        pitch = int(np.median(steps))
    return {'dots': out, 'active': active, 'pitch': pitch}


def image_layout_alpha(h, w, rect, radius, feather=IMAGE_LAYOUT_CUT_FEATHER):
    """Альфа (uint8 h×w) скруглённого прямоугольника rect=(x0, y0, x1, y1).

    Знаковое расстояние до скруглённого прямоугольника: внутри 255, снаружи
    0, кромка гасится за feather px — JPEG сдвигает край на ±1 px, и без
    фейда на живом фоне под вырезом видна нитка.
    """
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    hx, hy = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    qx = np.abs(np.arange(w, dtype=np.float32) - cx) - (hx - radius)
    qy = np.abs(np.arange(h, dtype=np.float32) - cy) - (hy - radius)
    dx = np.maximum(qx, 0.0)[None, :]
    dy = np.maximum(qy, 0.0)[:, None]
    d = (np.minimum(np.maximum(qx[None, :], qy[:, None]), 0.0)
         + np.hypot(dx, dy) - radius)
    return np.clip(255.0 * (0.5 - d / feather), 0.0, 255.0).astype(np.uint8)


def image_layout_cutout(src, out, rect, radius,
                        feather=IMAGE_LAYOUT_CUT_FEATHER, quality=90,
                        zero_hidden=True):
    """Вырез скруглённого прямоугольника: RGBA в out, формат по расширению.

    Картинка поверх «живого» CSS-фона: под вырезом остаётся только то, что
    должно быть непрозрачно. zero_hidden обнуляет RGB под полной прозрачно-
    стью — невидимые пиксели плавно градиента сжимаются в WebP/PNG хуже
    нуля (для 1080-ширины разница примерно 2.8 МБ против 150 КБ).
    """
    rgb = _to_uint8(src)
    h, w = rgb.shape[:2]
    alpha = image_layout_alpha(h, w, rect, radius, feather)
    rgba = np.dstack([rgb, alpha])
    if zero_hidden:
        rgba[alpha == 0, :3] = 0
    suffix = Path(out).suffix.lower()
    fmt = {'.webp': 'WebP', '.png': 'PNG'}.get(suffix)
    if fmt is None:
        raise ValueError(f'расширение {suffix}: ждут .webp или .png (альфа не для JPEG)')
    kwargs = {'quality': quality, 'method': 6} if fmt == 'WebP' else {'optimize': True}
    Image.fromarray(rgba).save(out, fmt, **kwargs)
    return {'file': str(out), 'size': (w, h), 'rect': tuple(rect),
            'opaque_pct': round(float((alpha > 0).mean()) * 100, 1),
            'bytes': Path(out).stat().st_size}


def _to_float(src) -> np.ndarray:
    """Кадр в float32 (h, w, 3): путь, PIL-картинка или массив."""
    if isinstance(src, np.ndarray):
        return src.astype(np.float32)
    if isinstance(src, Image.Image):
        return np.asarray(src.convert('RGB'), dtype=np.float32)
    return np.asarray(Image.open(src).convert('RGB'), dtype=np.float32)


def _to_uint8(src) -> np.ndarray:
    """Кадр в uint8 (h, w, 3): путь, PIL-картинка или массив."""
    if isinstance(src, np.ndarray):
        return src.astype(np.uint8)
    if isinstance(src, Image.Image):
        return np.asarray(src.convert('RGB'), dtype=np.uint8)
    return np.asarray(Image.open(src).convert('RGB'), dtype=np.uint8)


def _px(value, frac_default, extent):
    """Параметр пикселями или долями: value из двух чисел; float ≤ 1 — доля
    extent, иначе пиксели. None — умолчание-доля."""
    if value is None:
        v0, v1 = frac_default
        return int(round(v0 * extent)), int(round(v1 * extent))
    out = []
    for v, d in zip(value, frac_default):
        v = d if v is None else v
        out.append(int(round(v * extent)) if v <= 1.0 else int(v))
    return tuple(out)


def _px1(value, frac_default, extent):
    """То же для одного числа (якорь, граница зоны blend)."""
    if value is None:
        value = frac_default
    return int(round(value * extent)) if value <= 1.0 else int(value)


def _moving_avg(p, k):
    """Скользящее среднее с постоянными краями (окно не подмешивает соседние
    области профиля на границах)."""
    if k <= 1:
        return p
    kernel = np.ones(k, dtype=np.float32) / k
    pad = k // 2
    q = np.concatenate([np.full(pad, p[0], dtype=np.float32), p,
                        np.full(pad, p[-1], dtype=np.float32)])
    return np.convolve(q, kernel, mode='valid')[: p.shape[0]]


def main() -> None:
    ap = argparse.ArgumentParser(description='Геометрия разметки в кадре')
    ap.add_argument('command', choices=['bgfield', 'edge', 'dots', 'cutout'])
    ap.add_argument('files', nargs='+')
    ap.add_argument('--xy', action='append',
                    help='bgfield: точка x,y, где нужен цвет поля (повторяемо)')
    ap.add_argument('--band', help='edge: окно поперёк оси x0,x1|y0,y1; '
                                   'dots: строки y0,y1 (обязательно)')
    ap.add_argument('--axis', default='y', help="edge: 'y' (строки) или 'x'")
    ap.add_argument('--search', help='edge: диапазон поиска lo,hi пикселями')
    ap.add_argument('--tol', type=float, default=IMAGE_LAYOUT_DOTS_TOL)
    ap.add_argument('--rect', help='cutout: x0,y0,x1,y1 пикселями (обязательно)')
    ap.add_argument('--out', help='cutout: файл результата .webp|.png')
    ap.add_argument('--radius', type=float, default=48)
    ap.add_argument('--feather', type=float, default=IMAGE_LAYOUT_CUT_FEATHER)
    ns = ap.parse_args()
    try:
        if ns.command == 'bgfield':
            points = [tuple(int(v) for v in p.split(',')) for p in (ns.xy or [])]
            for f in ns.files:
                field = image_layout_bgfield(f)
                h, w = field.shape[:2]
                pts = points or [(w // 2, y) for y in
                                 (int(h * k) for k in (0.07, 0.25, 0.5, 0.75, 0.93))]
                for x, y in pts:
                    rgb = tuple(int(v) for v in np.round(field[y, x]))
                    print(f'{f}: поле фона в ({x},{y}) → rgb{rgb}')
        elif ns.command == 'edge':
            band = tuple(int(v) for v in ns.band.split(',')) if ns.band else None
            search = tuple(int(v) for v in ns.search.split(',')) if ns.search else None
            for f in ns.files:
                e = image_layout_edge(f, band=band, axis=ns.axis, search=search)
                print(f'{f}: {e["axis"]} — начало {e["rise_at"]} '
                      f'(подъём {e["rise_step"]}), конец {e["fall_at"]} '
                      f'(спад {e["fall_step"]})')
        elif ns.command == 'dots':
            if not ns.band:
                raise SystemExit('нужен --band: строки точки y0,y1')
            band = tuple(int(v) for v in ns.band.split(','))
            for f in ns.files:
                r = image_layout_dots(f, band, tol=ns.tol)
                line = ' '.join(f'{d["center"]}({d["width"]}px пик {d["peak"]})'
                                for d in r['dots'])
                act = (r['active'] if r['active'] is None
                       else r['active'] + 1)
                print(f'{f}: точек {len(r["dots"])}, активный слот {act}, '
                      f'шаг {r["pitch"]} — {line}')
        else:
            if not ns.rect or not ns.out:
                raise SystemExit('нужны --rect x0,y0,x1,y1 и --out файл.webp|.png')
            rect = tuple(int(v) for v in ns.rect.split(','))
            r = image_layout_cutout(ns.files[0], ns.out, rect,
                                    ns.radius, feather=ns.feather)
            print(f'{ns.files[0]} → {r["file"]}: {r["size"][0]}×{r["size"][1]}, '
                  f'непрозрачно {r["opaque_pct"]}%, {r["bytes"] // 1024} КБ')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')


if __name__ == '__main__':
    main()

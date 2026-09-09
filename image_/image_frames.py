"""
Скролл-кадры и анимация: смещение, склейка полотна, «шевелится ли кадр»,
монтаж кропов для визуальной проверки.

Отвечает на вопросы, которыми работают с сериями снимков одного экрана:
«на сколько строк уехал контент между кадрами» (`image_frames_delta`:
сравниваются high-pass профили строк, потому что фон-градиент привязан к
экрану и между кадрами различается — прямое сравнение сплывает на градиент),
«собери из серии со скроллом одно полотно» (`image_frames_stitch`: доклеивает
только дельту, стык растягивает косой альфой, спорные строки может брать из
позднего кадра — там, где ранний перекрыт тостом), «анимируется ли фон и куда
плывёт» (`image_frames_drift`: знаковое среднее сдвига по каналам по полосам —
в отличие от `image_.image_ diff` это не вердикт «тон/не тон», а направление
и величина: «фон светлеет в жёлтый», «карточка стоит на месте»), «на
сколько сдвинулась полоса и с какой скоростью» (`image_frames_shift`:
кросс-корреляция полосы по всему окну поиска — бегущая строка, карусель;
в отличие от `delta` смещение двустороннее и считается по пикселям полосы,
а не по ряду строк), и «покажи десять кропов одним взглядом»
(`image_frames_grid`: монтаж полос с разных кадров в одну картинку —
глазами десять файлов не сверить), и «за какой период анимации кадр
возвращается к первому» (`image_frames_cycle`: первый повтор первого кадра
по среднему по каналам — дыхание обоев, webp-луп, экран после тоста).

Грабли серии — `image_frames.md`.
"""
import argparse

import numpy as np
from PIL import Image

# Окно high-pass фильтра профилей строк: дольше самой плотной строки текста,
# короче заметных перепадов фона; поле профиля постоянные — фильтр не должен
# подмешивать края.
IMAGE_FRAMES_HIGHPASS = 151

# Полоса сравнения при поиске дельты: стартует с 1/6 высоты кадра — выше
# лежит прилипающий заголовок, который не ездит вместе с контентом, и окно
# длиной `window` строк.
IMAGE_FRAMES_BAND_FRAC = 1 / 6
IMAGE_FRAMES_WINDOW = 400

# Строки косой альфы на стыке: строки содержимого там и так совпадают,
# а ступенька градиента фона растягивается на 12 строк вместо одной.
IMAGE_FRAMES_BLEND = 12

# Полуокно поиска сдвига shift, пиксели. По x — с запасом на полосу, бегущую
# до ~90 px/s, между снимками в ~4 с (screencap короче ~1.5 с не даёт честного
# Δt — кадр «зависает» и рапортует ложный покой); по y — только на AA-тряску,
# горизонтальный контент по вертикали не ездит. Перебор профиля линеен, окно
# в 400 стоит доли секунды.
IMAGE_FRAMES_SHIFT_SEARCH = 400
IMAGE_FRAMES_SHIFT_SEARCH_Y = 4

# Полос поперёк кадра в сетке drift-полос; по умолчанию кадр режется на 8
# горизонтальных полос: асимметрия анимированного фона по горизонтали ниже,
# чем по вертикали.
IMAGE_FRAMES_DRIFT_ROWS = 8

# Допуск совпадения средних по каналу при поиске периода цикла, единицы
# канала: крупнее ±1 — шум повторного lossy-квантования не выдаётся за ход
# анимации; мельче — «повтор» не сойдётся даже на одном и том же кадре.
IMAGE_FRAMES_CYCLE_TOL = 2.0


def image_frames_delta(a, b, max_delta=None, window=IMAGE_FRAMES_WINDOW):
    """На сколько строк кадр b уехал вглубь контента относительно a.

    Вход — пути, PIL-картины или массивы одинаковой ширины. Сравнение по
    high-pass профилям: градиент подложки между кадрами разный, содержимое
    на верной дельте — одинаковое. Поиск грубый шагом 4, затем уточнение
    поштучно ±6 вокруг лучшего. Дельта неотрицательна: b — продолжение a.
    """
    pa = _highpass(_profile(a))
    pb = _highpass(_profile(b))
    h = pa.shape[0]
    band0 = max(h // 6, 300)
    cap = h - band0 - window - 1
    if max_delta is None:
        max_delta = cap
    max_delta = min(max_delta, cap)
    idx = np.arange(band0, band0 + window)
    best, best_dy = None, 0
    for dy in range(0, max_delta + 1, 4):
        err = float(np.abs(pb[idx] - pa[idx + dy]).mean())
        if best is None or err < best:
            best, best_dy = err, dy
    for dy in range(max(0, best_dy - 6), min(max_delta, best_dy + 6) + 1):
        err = float(np.abs(pb[idx] - pa[idx + dy]).mean())
        if err < best:
            best, best_dy = err, dy
    return int(best_dy)


def image_frames_stitch(paths, out, crop=None, start_row=0, quality=88,
                        blend=IMAGE_FRAMES_BLEND, prefer_last=False):
    """Полотно из серии скриншотов одного скролла; возвращает высоту.

    У каждого кадра отрезается окно [crop[0]:crop[1]] (панели статуса и
    вкладок в полотно не входят), дельта считается между соседними кадрами,
    доклеиваются только последние dy строк следующего. blend строк перед
    стыком перетекают косой альфой. prefer_last берёт перекрытие из позднего
    кадра: тост на раннем кадре висит поверх содержимого, поздний показывает
    те же строки чистыми — но только ниже start_row, строки выше в позднем
    кадре это сам прилипающий заголовок, а не уехавший контент.
    """
    frames = []
    for p in paths:
        a = np.asarray(Image.open(p).convert('RGB'), dtype=np.float32)
        if crop:
            a = a[crop[0]:crop[1]]
        frames.append(a)
    tall = frames[0][start_row:].copy()
    for prev, nxt in zip(frames, frames[1:]):
        dy = image_frames_delta(prev, nxt)
        if dy <= 0:
            continue
        if prefer_last:
            tall[dy:] = nxt[start_row: nxt.shape[0] - dy]
        new_rows = nxt[len(nxt) - dy:]
        b = int(min(blend, tall.shape[0], nxt.shape[0] - dy))
        if b > 0:
            dup = nxt[len(nxt) - dy - b: len(nxt) - dy]
            alpha = np.linspace(1 / (b + 1), b / (b + 1), b,
                                dtype=np.float32)[:, None, None]
            tall[-b:] = tall[-b:] * (1 - alpha) + dup * alpha
        tall = np.concatenate([tall, new_rows], axis=0)
    height = int(tall.shape[0])
    Image.fromarray(np.clip(np.round(tall), 0, 255).astype(np.uint8)).save(
        out, quality=quality)
    return {'file': str(out), 'height': height, 'frames': len(frames)}


def image_frames_drift(a, b, bands=None, rows=IMAGE_FRAMES_DRIFT_ROWS):
    """Чем кадр b отличается от кадра a по полосам: сдвиг анимации или покоя.

    Для каждой полосы (именованный rect или автоматическая сетка из `rows`
    горизонтальных полос): **знаковое** среднее смещение каждого канала
    (b − a) и |Δ| в среднем/максимум. Знак — это направление («низ уходит
    в синий» — B положителен), он и отвечает на вопрос «что вообще
    анимируется»: у спокойного контента mean_abs ≈ 0 при любом тон-дрейфе
    вокруг. Кадры должны быть по одному и тому же состоянию страницы, снятые
    без перезагрузки — навигация сбрасывает фазу анимации.
    """
    fa = np.asarray(Image.open(a).convert('RGB'), dtype=np.float32)
    fb = np.asarray(Image.open(b).convert('RGB'), dtype=np.float32)
    if fa.shape != fb.shape:
        raise ValueError(f'кадры разного размера: {fa.shape} и {fb.shape}')
    h, w = fa.shape[:2]
    if bands is None:
        bands = [(f'row{i + 1}', (0, h * i // rows, w, h * (i + 1) // rows))
                 for i in range(rows)]
    out = []
    for name, (x0, y0, x1, y1) in bands:
        da = fb[y0:y1, x0:x1] - fa[y0:y1, x0:x1]
        out.append({'name': name, 'box': (x0, y0, x1, y1),
                    'shift': tuple(int(round(v)) for v in da.mean(axis=(0, 1))),
                    'mean_abs': round(float(np.abs(da).mean()), 1),
                    'max_abs': int(np.abs(da).max())})
    return {'bands': out}


def image_frames_shift(a, b, band=None, search=IMAGE_FRAMES_SHIFT_SEARCH,
                       search_y=IMAGE_FRAMES_SHIFT_SEARCH_Y, dt=None) -> dict:
    """На сколько пикселей содержимое полосы кадра b уехало относительно a.

    Сравнение по **профилям полосы**, не по пикселям: текст строкой едет на
    субпиксель, глифы каждый кадр дорисовываются с новым AA, и пиксельная
    корреляция тонкого текста поверх цветного градиента почти не различает
    сдвиги (проверено на живом тикере: 2D-корреляция по пикселям дала −34 px
    при corr 0.37 там, где колоночный профиль находит −175 px при corr 0.91;
    невязка профилей на верном сдвиге 6% против ~30% вокруг). Профиль
    темноты колонок (сумма яркости по строкам полосы) сдвигу инвариантен:
    пик острый, идентичные кадры дают 1.00. Профиль строк — dy.

    Args:
        a, b: пути, PIL-картины или массивы одного размера; b — поздний кадр.
        band: rect (x0, y0, x1, y1) в координатах кадра — полоса движения
            (бегущая строка, карусель). Без неё центральная треть высоты:
            прилипающий заголовок и панель вкладок не ездят и разбавляют
            сигнал. Полоса должна быть ШИРЕ шага движения хотя бы вдвое,
            иначе overlap мал и corr неострая.
        search: полуокно поиска по x, px.
        search_y: полуокно поиска по y, px.
        dt: секунды между кадрами — тогда в ответе есть скорость px/s.

    Returns:
        {'band', 'dx', 'dy', 'corr', 'dt', 'px_per_s'} — dx > 0 значит,
        содержимое в b уехало ВПРАВО на dx (бегущая строка даёт dx < 0);
        'px_per_s': {'x', 'y'} при dt, иначе None; corr — корреляция
        колоночных профилей на лучшем сдвиге (< 0.7 — пик, скорее всего,
        алиасинг или сдвиг не влез в --search, см. грабли).

    Грабли честного Δt: `screencap` короткими тиражами «зависает» — соседние
    кадры байт-в-байт идентичны, corr=1.00 при dx=0 выглядит как «стоит», а
    на деле это тот же кадр; скорость мерить цепочкой пар с Δt ≥ ~1.5 с
    (серию снимает `adb_.adb_burst`) и считать сумма(dx)/сумма(Δt) —
    одиночная пара на длинном шаге даёт алиасинг, а на коротком — зависание.
    Если corr < 0.7 — за окно поиска сдвиг не влез, увеличивай search.
    """
    fa = np.asarray(_to_float(a), dtype=np.float64)
    fb = np.asarray(_to_float(b), dtype=np.float64)
    if fa.shape != fb.shape:
        raise ValueError(f'кадры разного размера: {fa.shape} и {fb.shape}')
    h, w = fa.shape[:2]
    if band is None:
        band = (0, h // 3, w, 2 * h // 3)
    x0, y0, x1, y1 = (int(v) for v in band)
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError(f'полоса {tuple(band)} не лежит в кадре {w}x{h}')
    # Rec.601, не среднее каналов: подложка тикера цветная (зелёный→голубой),
    # равные веса уводят профиль и corr падает с 0.99 до 0.89.
    luma_w = np.array([0.299, 0.587, 0.114])
    reg = fa[y0:y1, x0:x1] @ luma_w
    reg_b = fb[y0:y1, x0:x1] @ luma_w
    corr_x, s_x = _shift_1d(reg.mean(axis=0), reg_b.mean(axis=0), search)
    corr_y, s_y = _shift_1d(reg.mean(axis=1), reg_b.mean(axis=1), search_y)
    dx, dy = -int(s_x), -int(s_y)  # профиль совпал со сдвигом вперёд = контент уехал влево
    result = {'band': (x0, y0, x1, y1), 'dx': dx, 'dy': dy,
              'corr': round(corr_x, 4), 'dt': dt, 'px_per_s': None}
    if dt:
        result['px_per_s'] = {'x': round(dx / dt, 2), 'y': round(dy / dt, 2)}
    return result


def image_frames_grid(items, out, cols=3, scale=1.0, pad=12):
    """Монтаж кропов с разных кадров в одну картинку для визуальной сверки.

    items — список (путь, box) либо путей; box = (x0, y0, x1, y1) — полоса,
    которая нужна с этого кадра (без box — весь кадр). Плитки масштабируются
    `scale` и раскладываются по строкам; тёмная подложка между ними — чтобы
    шов между разными кадрами не выглядел продолжением картинки. Десять
    «низов слайдов» или «строк точек со всех страниц» одним read — только
    так и сверяются.
    """
    tiles = []
    for item in items:
        path, box = item if isinstance(item, (tuple, list)) else (item, None)
        im = Image.open(path).convert('RGB')
        if box:
            im = im.crop(tuple(int(v) for v in box))
        if scale != 1.0:
            im = im.resize((max(1, int(im.width * scale)),
                            max(1, int(im.height * scale))), Image.LANCZOS)
        tiles.append(im)
    if not tiles:
        raise ValueError('пустой список плиток')
    rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
    row_h = [max(t.height for t in r) for r in rows]
    width = max(sum(t.width for t in r) + pad * (len(r) - 1) for r in rows)
    canvas = Image.new('RGB', (width, sum(row_h) + pad * (len(rows) - 1)),
                       (32, 32, 32))
    y = 0
    for r, rh in zip(rows, row_h):
        x = 0
        for t in r:
            canvas.paste(t, (x, y))
            x += t.width + pad
        y += rh + pad
    canvas.save(out)
    return {'file': str(out), 'tiles': len(tiles),
            'size': (canvas.width, canvas.height)}


def image_frames_cycle(paths, interval=None, tol=IMAGE_FRAMES_CYCLE_TOL,
                       rect=None) -> dict:
    """
    Период цикла анимации по серии кадров — первый повтор первого кадра.

    Кадры снимают с равным шагом, период — первый кадр после нулевого, чей
    средний по каналам совпал с нулевым в допуске `tol`: «дыхание» обоев,
    webp-луп, возврат экрана после тоста. Среднее по окну вместо попиксельного
    сравнения — осознанный компромисс: вернувшийся кадр совпадает и в среднем,
    а среднее не сходит с ума от шума сжатия живых скриншотов. `period_frames`
    1 — соседние кадры уже одинаковы: покой или шаг выборки мельче хода
    анимации. None честнее нуля: за эту серию повтор не наступил, период
    длиннее серии — а не «цикла нет». Окном `rect` вырезают движущиеся
    оверлеи (часы, счётчик), которые портят среднее и прячут возврат фона.

    Args:
        paths: упорядоченные кадры серии, минимум два.
        interval: шаг съёмки, секунды; с ним приходит `period_s`.
        tol: допуск совпадения средних по каналу, 0-255.
        rect: (x0, y0, x1, y1) — окно кадра без движущихся оверлеев.

    Returns:
        {'frames', 'period_frames': int|None, 'period_s': float|None,
         'means': [(r, g, b)] — средние по кадру, ряд видно глазами}.

    Raises:
        ValueError: кадров меньше двух — период по одному кадру не считается.
    """
    if len(paths) < 2:
        raise ValueError('нужны хотя бы два кадра серии')
    means = []
    for p in paths:
        a = _to_float(p)
        if rect:
            a = a[int(rect[1]):int(rect[3]), int(rect[0]):int(rect[2])]
        means.append(a.mean(axis=(0, 1)))
    period = next((k for k in range(1, len(means))
                   if float(np.abs(means[k] - means[0]).max()) <= tol), None)
    return {'frames': len(means), 'period_frames': period,
            'period_s': (round(period * interval, 2)
                         if period is not None and interval else None),
            'means': [tuple(round(float(v), 2) for v in m) for m in means]}


def _profile(img) -> np.ndarray:
    """Профиль яркости строк: среднее по ширине, float32."""
    a = np.asarray(_to_float(img), dtype=np.float32)
    return a.reshape(a.shape[0], -1).mean(axis=1)


def _highpass(p, k=IMAGE_FRAMES_HIGHPASS) -> np.ndarray:
    """Минус скользящее среднее: экранная подложка-градиент вычитается,
    остаются строки содержимого. Поля — постоянные."""
    kernel = np.ones(k, dtype=np.float32) / k
    pad = k // 2
    q = np.concatenate([np.full(pad, p[0], dtype=np.float32), p,
                        np.full(pad, p[-1], dtype=np.float32)])
    return p - np.convolve(q, kernel, mode='valid')[: p.shape[0]]


def _to_float(img) -> np.ndarray:
    """Путь или PIL-картина → массив float32; массив проходит как есть."""
    if isinstance(img, np.ndarray):
        return img
    if isinstance(img, Image.Image):
        return np.asarray(img.convert('RGB'), dtype=np.float32)
    return np.asarray(Image.open(img).convert('RGB'), dtype=np.float32)


def _shift_1d(pa: np.ndarray, pb: np.ndarray, search: int) -> tuple:
    """Лучший целочисленный сдвиг профиля pb относительно pa: (corr, s).

    Перебор −search…+search с нормировкой НА ПЕРЕКРЫТИИ: нормировка целым
    профилем занижала бы corr на краях окна ровно там, где лежит верный
    большой сдвиг, и пик уезжал бы к нулю.
    """
    best = (-2.0, 0)
    n = pa.shape[0]
    for s in range(-search, search + 1):
        aa, bb = (pa[s:], pb[:n - s]) if s >= 0 else (pa[:n + s], pb[-s:])
        aa = aa - aa.mean()
        bb = bb - bb.mean()
        e = np.sqrt((aa ** 2).sum() * (bb ** 2).sum())
        c = float((aa * bb).sum() / e) if e else 0.0
        if c > best[0]:
            best = (c, s)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description='Скролл-кадры и анимация')
    ap.add_argument('command',
                    choices=['delta', 'stitch', 'drift', 'shift', 'grid', 'cycle'])
    ap.add_argument('files', nargs='+',
                    help='delta/drift/shift: два кадра; stitch/cycle: серия; '
                         "grid: path или path@x0,y0,x1,y1 (повторяемо)")
    ap.add_argument('--out', help='stitch/grid: файл результата')
    ap.add_argument('--crop', help='stitch: окно кадра top,bottom')
    ap.add_argument('--start-row', type=int, default=0)
    ap.add_argument('--blend', type=int, default=IMAGE_FRAMES_BLEND)
    ap.add_argument('--prefer-last', action='store_true')
    ap.add_argument('--band', action='append',
                    help='drift: имя:x0,y0,x1,y1 (повторяемо; без него — '
                         'сетка полос); shift: одна полоса x0,y0,x1,y1')
    ap.add_argument('--search', type=int, default=IMAGE_FRAMES_SHIFT_SEARCH,
                    help='shift: полуокно поиска по x, px')
    ap.add_argument('--search-y', type=int, default=IMAGE_FRAMES_SHIFT_SEARCH_Y,
                    help='shift: полуокно поиска по y, px')
    ap.add_argument('--dt', type=float,
                    help='shift: секунды между кадрами — для скорости px/s')
    ap.add_argument('--rows', type=int, default=IMAGE_FRAMES_DRIFT_ROWS)
    ap.add_argument('--cols', type=int, default=3)
    ap.add_argument('--scale', type=float, default=1.0)
    ap.add_argument('--interval', type=float,
                    help='cycle: секунды между кадрами съёмки — для периода в секундах')
    ap.add_argument('--tol', type=float, default=IMAGE_FRAMES_CYCLE_TOL,
                    help='cycle: допуск совпадения средних по каналу')
    ap.add_argument('--rect', help='cycle: x0,y0,x1,y1 окно без движущихся оверлеев')
    ns = ap.parse_args()
    try:
        if ns.command == 'delta':
            if len(ns.files) != 2:
                raise SystemExit('нужно ровно два кадра')
            print(f'дельта {ns.files[0]} → {ns.files[1]}: '
                  f'{image_frames_delta(ns.files[0], ns.files[1])} строк')
        elif ns.command == 'stitch':
            if not ns.out:
                raise SystemExit('нужен --out файл полотна')
            crop = tuple(int(v) for v in ns.crop.split(',')) if ns.crop else None
            r = image_frames_stitch(ns.files, ns.out, crop=crop,
                                    start_row=ns.start_row, blend=ns.blend,
                                    prefer_last=ns.prefer_last)
            print(f'{r["file"]}: {r["height"]} строк из {r["frames"]} кадров')
        elif ns.command == 'drift':
            if len(ns.files) != 2:
                raise SystemExit('нужно ровно два кадра одного экрана')
            bands = None
            if ns.band:
                bands = []
                for spec in ns.band:
                    name, box = spec.split(':', 1)
                    bands.append((name, tuple(int(v) for v in box.split(','))))
            d = image_frames_drift(ns.files[0], ns.files[1], bands=bands,
                                   rows=ns.rows)
            for z in d['bands']:
                s = z['shift']
                print(f'{z["name"]:>6} {z["box"]}: сдвиг ({s[0]:+d},{s[1]:+d},{s[2]:+d}) '
                      f'|Δ| среднее {z["mean_abs"]:>5}, max {z["max_abs"]}')
        elif ns.command == 'shift':
            if len(ns.files) != 2:
                raise SystemExit('нужно ровно два кадра')
            band = None
            if ns.band:
                spec = ns.band[-1].rpartition(':')[2]
                band = tuple(int(v) for v in spec.split(','))
            r = image_frames_shift(ns.files[0], ns.files[1], band=band,
                                   search=ns.search, search_y=ns.search_y,
                                   dt=ns.dt)
            line = (f"смещение b относительно a: dx {r['dx']:+d} px, "
                    f"dy {r['dy']:+d} px, корреляция {r['corr']}")
            if r['corr'] < 0.7:
                line += ' — низкая, возможен алиасинг или мало --search'
            if r['px_per_s']:
                line += (f"; скорость {r['px_per_s']['x']:+g} px/с "
                         f"при dt {r['dt']:g} с")
            print(line)
        elif ns.command == 'cycle':
            rect = tuple(int(v) for v in ns.rect.split(',')) if ns.rect else None
            r = image_frames_cycle(ns.files, interval=ns.interval,
                                   tol=ns.tol, rect=rect)
            if r['period_frames'] is None:
                print(f"цикла не видно за серию из {r['frames']} кадров")
            else:
                line = f"период {r['period_frames']} кадров"
                if r['period_s'] is not None:
                    line += f" = {r['period_s']} с"
                print(line)
        else:
            if not ns.out:
                raise SystemExit('нужен --out файл монтажа')
            items = []
            for spec in ns.files:
                path, _, box = spec.partition('@')
                items.append((path, box.split(',') if box else None))
            r = image_frames_grid(items, ns.out, cols=ns.cols, scale=ns.scale)
            print(f'{r["file"]}: плиток {r["tiles"]}, кадр {r["size"][0]}×{r["size"][1]}')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')


if __name__ == '__main__':
    main()

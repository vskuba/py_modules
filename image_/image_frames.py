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
и величина: «фон светлеет в жёлтый», «карточка стоит на месте»), и «покажи
десять кропов одним взглядом» (`image_frames_grid`: монтаж полос с разных
кадров в одну картинку — глазами десять файлов не сверить).

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

# Полос поперёк кадра в сетке drift-полос; по умолчанию кадр режется на 8
# горизонтальных полос: асимметрия анимированного фона по горизонтали ниже,
# чем по вертикали.
IMAGE_FRAMES_DRIFT_ROWS = 8


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


def main() -> None:
    ap = argparse.ArgumentParser(description='Скролл-кадры и анимация')
    ap.add_argument('command', choices=['delta', 'stitch', 'drift', 'grid'])
    ap.add_argument('files', nargs='+',
                    help='delta/drift: два кадра; stitch: серия; '
                         "grid: path или path@x0,y0,x1,y1 (повторяемо)")
    ap.add_argument('--out', help='stitch/grid: файл результата')
    ap.add_argument('--crop', help='stitch: окно кадра top,bottom')
    ap.add_argument('--start-row', type=int, default=0)
    ap.add_argument('--blend', type=int, default=IMAGE_FRAMES_BLEND)
    ap.add_argument('--prefer-last', action='store_true')
    ap.add_argument('--band', action='append',
                    help='drift: имя:x0,y0,x1,y1 (повторяемо; без него — '
                         'сетка полос)')
    ap.add_argument('--rows', type=int, default=IMAGE_FRAMES_DRIFT_ROWS)
    ap.add_argument('--cols', type=int, default=3)
    ap.add_argument('--scale', type=float, default=1.0)
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

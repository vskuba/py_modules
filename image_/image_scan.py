"""
Линейные замеры кадра: однотонные отрезки по разрезу, строки текста с шагом,
габарит глифов в окне, батч контрольных точек и сверка клона с оригиналом.

Инструменты «где граница» для сверки свёрстанного со снимком. По одиночной
вертикали/горизонтали кадра: где начинается и заканчивается белая панель
(`image_scan_runs` — список однотонных отрезков с их концами), с каким шагом
идут строки текста и где их центры (`image_scan_rows` — профиль тёмных
строк, по нему же сверяют pitch меню), какой высоты реально отрисовались
глифы в окне (`image_scan_glyph` — капитель/ascender, по нему подбирают
`font-size` под замер с телефона: «капитель 38 px — это 4.81 vw»). Список
контрольных точек мерится разом (`image_scan_windows`), а сверка кадра-клона
с оригиналом, сдвинутым на константу, — `image_scan_windows_diff`. Габарит
пикселей заданного тона по всему кадру — `image_scan_bbox`: куда влезла
тестовая картинка в слот, дотянуто ли стирание до фона.

Чем эти замеры отличаются от `image_.image_measure`: тот отвечает «какой тон
в окне» (модальная медиана), эти — «где идут границы вдоль линии». Окно
замера по-прежнему берут над безглифной зоной, иначе модой станет текст.

Пороговые замеры по пикселям честны только на статичном кадре: кадр, снятый
посреди CSS-перехода, даёт полупрозрачный слой (серая «белая» карточка на
233 вместо 255), и отрезки сгорают — снимайте с бюджетом виртуального
времени (`web_/web_shot`).
"""
import argparse

import numpy as np
from PIL import Image

# Допуск тона к чистому белому/чёрному: JPEG и даунсемплинг размывают край,
# «белый» фон на снимке телефона — 250-254.
IMAGE_SCAN_TOL = 8

# Минимальная длина отрезка, пиксели: короче — шум сжатия или глиф-точка.
IMAGE_SCAN_MIN_RUN = 20

# Порог «тёмный пиксель» (средний по каналам) для текста на светлом.
IMAGE_SCAN_THRESH = 120

# Разрывы между тёмными строками, объединяемые в одну строку: антиалиасинг
# и межстрочные лигатуры рвут профиль на 1-2 пиксельные щели.
IMAGE_SCAN_ROW_GAP = 2

# Допуск остатка в сверке окон, px: размытый снимок телефона и чистый рендер
# дают ink-рамку, разъезжающуюся на 1-2 px при одинаковой вёрстке; больше —
# реальное расхождение.
IMAGE_SCAN_DIFF_TOL = 2


def image_scan_runs(path, axis='y', pos=None, tone='white',
                    tol=IMAGE_SCAN_TOL, min_len=IMAGE_SCAN_MIN_RUN,
                    rect=None) -> dict:
    """
    Однотонные отрезки вдоль вертикального или горизонтального разреза кадра.

    Так ищут края панелей по одной линии: «белый лист начинается тут,
    кончается там, ниже — щель, потом пилюля». `tone` — 'white', 'black',
    'chroma' или hex («#EDEDED») с допуском `tol` по каждому каналу.

    Args:
        path: картинка.
        axis: 'y' — разрез-колонка (отрезки по вертикали), 'x' — разрез-строка.
        pos: координата разреза поперёк (для 'y' — x колонки);
            пусто — центр кадра (у краёв врут скруглённые углы панелей).
        tone: 'white' | 'black' | 'chroma' (любой цветной) | '#rrggbb'.
        tol: допуск по каналу, 0-255.
        min_len: минимальная длина отрезка, пиксели.
        rect: (x0, y0, x1, y1) — ограничить зону поиска вдоль оси разреза.

    Returns:
        {'file', 'axis', 'pos', 'tone', 'runs': [(начало, конец)], 'count'};
        концы включительно, в координатах целого кадра.
    """
    if axis not in ('x', 'y'):
        raise ValueError("axis — 'y' (колонка) или 'x' (строка)")
    img = np.asarray(Image.open(path).convert('RGB'), dtype=np.int32)
    h, w = img.shape[:2]
    if axis == 'y':
        pos = w // 2 if pos is None else int(pos)
        line, lo, hi = img[:, pos, :], *(rect[1:3] if rect else (0, h))
    else:
        pos = h // 2 if pos is None else int(pos)
        line, lo, hi = img[pos, :, :], *(rect[0:2] if rect else (0, w))
    lo, hi = int(lo), min(int(hi), line.shape[0])
    mask = _tone_mask(line, tone, tol)

    runs, start = [], None
    for i in range(lo, hi):
        if mask[i] and start is None:
            start = i
        elif not mask[i] and start is not None:
            if i - start >= min_len:
                runs.append((start, i - 1))
            start = None
    if start is not None and hi - start >= min_len:
        runs.append((start, hi - 1))
    return {'file': path, 'axis': axis, 'pos': pos, 'tone': tone,
            'runs': runs, 'count': len(runs)}


def image_scan_bbox(path, tone='chroma', tol=IMAGE_SCAN_TOL, rect=None,
                    alpha_thresh=0) -> dict:
    """
    Габарит пикселей заданного тона по всему кадру (или по окну `rect`).

    Ответ на «где в кадре цветное», когда координаты неизвестны: залили слот
    тестовой картинкой — bbox покажет, куда она реально легла; стёрли
    запечённый объект — bbox остатков скажет, дотянуто ли до фона. Тон по
    умолчанию 'chroma': фон карточек серый, у серого хроматика нулевая, так
    что находится именно содержимое, а не тонкая вариация подложки. Окном
    отсекают цветные оверлеи интерфейса (кнопки, баннеры).

    Конвертация в RGB, которую делает эта функция, оставляет под полностью
    прозрачными пикселями их погребённый RGB: тон 'black' на RGBA-иконке
    считает чёрными и невидимые прозрачные углы. Для иконок и вырезок зовут с
    `alpha_thresh` — маска тона скрещивается с альфой. Порог берут серединой
    (128): антиалиасинг даёт краям дробную альфу, и без порога AA-ореол
    раздувает габарит на процент — ровно на тот, на котором сверяют иконку.

    Args:
        path: картинка.
        tone: 'white' | 'black' | 'chroma' | '#rrggbb' (см. `_tone_mask`).
        tol: допуск по каналу, 0-255.
        rect: (x0, y0, x1, y1) — зона поиска в координатах кадра.
        alpha_thresh: 0 — альфу не смотреть (прежнее поведение); 1-255 —
            считать тональными только пиксели с альфой строго выше порога.

    Returns:
        {'file', 'tone', 'box': (x0, y0, x1, y1)|None, 'count',
         'share_w', 'share_h'}; концы включительно; box None, если пикселей
        тона нет вовсе; share — доля габарита по ширине и высоте канвы (0 без
        box): им проверяют иконку — «плашка 0.79 канвы».
    """
    pil = Image.open(path)
    solid = None
    if alpha_thresh:
        rgba = np.asarray(pil.convert('RGBA'), dtype=np.int32)
        img = rgba[..., :3]
        solid = rgba[..., 3] > int(alpha_thresh)
    else:
        img = np.asarray(pil.convert('RGB'), dtype=np.int32)
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in rect) if rect else (0, 0, w, h)
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
    mask = _tone_mask(img[y0:y1, x0:x1], tone, tol)
    if solid is not None:
        mask &= solid[y0:y1, x0:x1]
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return {'file': path, 'tone': tone, 'box': None, 'count': 0,
                'share_w': 0.0, 'share_h': 0.0}
    box = (x0 + int(xs.min()), y0 + int(ys.min()),
           x0 + int(xs.max()), y0 + int(ys.max()))
    return {'file': path, 'tone': tone, 'box': box, 'count': int(ys.size),
            'share_w': round((box[2] - box[0] + 1) / w, 4),
            'share_h': round((box[3] - box[1] + 1) / h, 4)}


def image_scan_rows(path, rect=None, thresh=IMAGE_SCAN_THRESH,
                    min_height=3) -> dict:
    """
    Строки текста в окне: полосы тёмных рядов, центры и шаг (median).

    Шаг — медиана разностей центров: она не портится одним потерянным или
    лишним рядом (в меню между строками бывает разделитель, а первая строка
    — с заголовком другой высоты).

    Args:
        path: картинка.
        rect: (x0, y0, x1, y1) — окно строго по колонке текста; иначе
            соберутся и тёмные пиксели фона/иконок.
        thresh: порог тёмного (среднее по каналам).
        min_height: строки тоньше — антиалиасинг, выбрасываются.

    Returns:
        {'file', 'rows': [(y0, y1)], 'centers': [...], 'pitch': float|None}
        — pitch в пикселях, None если строк меньше двух.
    """
    img = np.asarray(Image.open(path).convert('RGB'), dtype=np.int32)
    h, w = img.shape[:2]
    y0, y1 = (rect[1], rect[3]) if rect else (0, h)
    x0, x1 = (rect[0], rect[2]) if rect else (0, w)
    dark = (img[y0:y1, x0:x1].mean(axis=2) < thresh).sum(axis=1) > 0
    dark = _merge_gaps(dark, IMAGE_SCAN_ROW_GAP)

    rows, start = [], None
    for i, v in enumerate(dark):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_height:
                rows.append((y0 + start, y0 + i - 1))
            start = None
    if start is not None and len(dark) - start >= min_height:
        rows.append((y0 + start, y0 + len(dark) - 1))
    centers = [round((a + b) / 2, 1) for a, b in rows]
    pitch = None
    if len(centers) >= 2:
        pitch = float(np.median(np.diff(centers)))
    return {'file': path, 'rows': rows, 'centers': centers, 'pitch': pitch}


def image_scan_glyph(path, rect, thresh=IMAGE_SCAN_THRESH - 10) -> dict:
    """
    Габарит тёмных глифов в окне — фактическая высота нарисованного текста.

    Ей сверяют `font-size` с замером капители с телефона: размер шрифта
    больше капители примерно в 1.4 раза (у Roboto ~0.71 em, у шрифтов
    headless-рендера — другое), поэтому калибруют именно по отрисованной
    высоте, а не по «44 px = капитель» на глаз.

    Args:
        path: картинка.
        rect: (x0, y0, x1, y1) — окно, содержащее только нужный текст;
            точка над «і» и «д»-хвостики входят в габарит — окно ставят
            по строке без выносных элементов, когда нужна капитель.
        thresh: порог тёмного.

    Returns:
        {'file', 'x0', 'y0', 'x1', 'y1', 'w', 'h', 'count'}.

    Raises:
        ValueError: в окне нет тёмных пикселей (окно мимо текста или
            порог ниже тона текста).
    """
    img = np.asarray(Image.open(path).convert('RGB'), dtype=np.int32)
    x0, y0, x1, y1 = (int(v) for v in rect)
    reg = img[y0:y1, x0:x1]
    ys, xs = np.where(reg.mean(axis=2) < thresh)
    if ys.size == 0:
        raise ValueError(f'в окне {tuple(rect)} нет тёмных пикселей при '
                         f'пороге {thresh} — окно мимо текста или порог мал')
    gx0, gy0, gx1, gy1 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    return {'file': path, 'x0': x0 + gx0, 'y0': y0 + gy0,
            'x1': x0 + gx1, 'y1': y0 + gy1,
            'w': gx1 - gx0 + 1, 'h': gy1 - gy0 + 1, 'count': int(ys.size)}


def image_scan_windows(path, windows, thresh=IMAGE_SCAN_THRESH) -> dict:
    """
    Батч габаритов тёмного инка по именованным окнам — за один проход по кадру.

    Сверка свёрстанного со снимком всегда идёт по списку контрольных точек
    (заголовок, номер, строки таблицы), а не по одному окну: `image_scan_glyph`
    на пустом окне бросает ValueError, и проверка из десятка окон рассыпается
    на десять вызовов с обёртками. Здесь пустое окно — `None` в ответе:
    «инка нет» — это результат проверки, а не исключение.

    Args:
        path: картинка.
        windows: {имя: (x0, y0, x1, y1)} — окна в координатах кадра.
        thresh: порог тёмного, как в `image_scan_glyph`.

    Returns:
        {'file', 'windows': {имя: {'x0','y0','x1','y1','w','h','count'} | None}}
    """
    img = np.asarray(Image.open(path).convert('RGB'), dtype=np.int32)
    out = {}
    for name, rect in windows.items():
        out[name] = _ink_bbox(img, rect, thresh)
    return {'file': path, 'windows': out}


def image_scan_windows_diff(path_a, path_b, windows, offset='auto',
                            thresh=IMAGE_SCAN_THRESH,
                            tol=IMAGE_SCAN_DIFF_TOL) -> dict:
    """
    Сверка двух кадров по именованным окнам с константным смещением.

    Кадры одного экрана часто лежат сдвинуто (скриншот телефона со статус-баром
    против headless-рендера, кадр до и после правки разметки): сверяют не
    пиксели, а ink-рамку каждой контрольной точки после снятия константы.
    Смещение берут медианой разностей верхних левых углов по окнам с инком на
    обеих сторонах — медиана не портится одним съехавшим окном; его же видно
    по большому `resid` в отчёте.

    Args:
        path_a, path_b: картинки; b — сверяемый кадр (рендер к оригиналу).
        windows: {имя: (x0, y0, x1, y1)} — окна в координатах кадра a; для b
            окно сдвигается на offset.
        offset: 'auto' — медианное смещение (с перезамером по сдвинутому
            окну, пока не сойдётся); None — считать нулевым; (dx, dy) —
            задано вручную (например, снято с одного замера).
        thresh: порог тёмного для обоих кадров.
        tol: допустимый остаток по каждой оси, пиксели.

    Returns:
        {'file_a', 'file_b', 'offset': (dx, dy) | None, 'tol',
         'ok': bool, 'bad': [имена],
         'windows': {имя: {'a', 'b',            # габариты или None
                           'delta': (dx, dy) | None,   # b − a как есть
                           'resid': (dx, dy) | None,   # delta − offset
                           'ok': bool}}}
        Пустое окно с обеих сторон ok (согласованное отсутствие); пустое с
        одной — не ok, это пропажа или лишнее содержимое.
    """
    img_a = np.asarray(Image.open(path_a).convert('RGB'), dtype=np.int32)
    img_b = np.asarray(Image.open(path_b).convert('RGB'), dtype=np.int32)
    base = (0, 0) if offset in ('auto', None) else offset
    boxes = _pair_boxes(img_a, img_b, windows, thresh, base)
    if offset == 'auto':
        # Сдвинутое содержимое может оказаться обрезано краем несдвинутого
        # окна (замер покажет край окна вместо края инка): медиану пересчитывают
        # по сдвинутым окнам, пока не сойдётся. Сходится за один-два прохода.
        for _ in range(3):
            new = _median_offset(boxes) or (0, 0)
            if new == base:
                break
            base = new
            boxes = _pair_boxes(img_a, img_b, windows, thresh, base)
        offset = base

    result, bad = {}, []
    for name, (a, b) in boxes.items():
        if a is None or b is None:
            entry = {'a': a, 'b': b, 'delta': None, 'resid': None,
                     'ok': a is None and b is None}
        else:
            delta = (b['x0'] - a['x0'], b['y0'] - a['y0'])
            resid = (delta[0] - base[0], delta[1] - base[1])
            entry = {'a': a, 'b': b, 'delta': delta, 'resid': resid,
                     'ok': abs(resid[0]) <= tol and abs(resid[1]) <= tol}
        result[name] = entry
        if not entry['ok']:
            bad.append(name)
    return {'file_a': path_a, 'file_b': path_b,
            'offset': tuple(offset) if offset else None, 'tol': tol,
            'ok': not bad, 'bad': bad, 'windows': result}


def _tone_mask(line, tone, tol):
    """Булева «линия попадает в тон» по всем пикселям строки/колонки.

    'chroma' — «цветной»: размах каналов больше допуска. Серый фон кадра
    (страница, карточка, заглушка) даёт нулевую хроматику, поэтому chroma
    находит на нём именно цветное содержимое, не подвязываясь под конкретный
    hex — замер тон-в-тон для этого и есть, что цвет неизвестен заранее.

    Канал — последняя ось, редукции по axis=-1: функцию вызывают и линией
    (n, 3), и полем кадра (h, w, 3) — image_scan_bbox.
    """
    if tone == 'white':
        return line.min(axis=-1) >= 255 - tol
    if tone == 'black':
        return line.max(axis=-1) <= tol
    if tone == 'chroma':
        return line.max(axis=-1) - line.min(axis=-1) > tol
    if len(tone) == 7 and tone[0] == '#':
        rgb = np.array([int(tone[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.int32)
        return (np.abs(line - rgb) <= tol).all(axis=-1)
    raise ValueError(
        f"tone — 'white', 'black', 'chroma' или '#rrggbb', получено {tone!r}")


def _merge_gaps(mask, gap):
    """Склеить разрывы короче gap: AA-щели внутри глифов не границы строк."""
    out = mask.copy()
    start = None
    for i, v in enumerate(mask):
        if not v and start is None:
            start = i
        elif v and start is not None:
            if i - start <= gap:
                out[start:i] = True
            start = None
    return out


def _ink_bbox(img, rect, thresh):
    """Габарит тёмного в окне или None; окно ограничивается кадром молча —
    при смещённой сверке окно легитимно вылезает за край."""
    h, w = img.shape[:2]
    x0 = max(int(rect[0]), 0)
    y0 = max(int(rect[1]), 0)
    x1 = min(int(rect[2]), w)
    y1 = min(int(rect[3]), h)
    if x0 >= x1 or y0 >= y1:
        return None
    reg = img[y0:y1, x0:x1]
    ys, xs = np.where(reg.mean(axis=2) < thresh)
    if ys.size == 0:
        return None
    gx0, gy0 = x0 + int(xs.min()), y0 + int(ys.min())
    gx1, gy1 = x0 + int(xs.max()), y0 + int(ys.max())
    return {'x0': gx0, 'y0': gy0, 'x1': gx1, 'y1': gy1,
            'w': gx1 - gx0 + 1, 'h': gy1 - gy0 + 1, 'count': int(ys.size)}


def _shifted(rect, offset):
    dx, dy = (int(v) for v in offset)
    return (rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy)


def _median_offset(boxes):
    """Медиана разностей левых верхних углов по окнам с инком с двух сторон."""
    deltas = [(b['x0'] - a['x0'], b['y0'] - a['y0'])
              for a, b in boxes.values() if a is not None and b is not None]
    if not deltas:
        return None
    return (int(np.median([d[0] for d in deltas])),
            int(np.median([d[1] for d in deltas])))


def _pair_boxes(img_a, img_b, windows, thresh, base):
    """Пары габаритов (a, b): окно a как задано, окно b сдвинуто на base."""
    return {name: (_ink_bbox(img_a, rect, thresh),
                   _ink_bbox(img_b, _shifted(rect, base), thresh))
            for name, rect in windows.items()}


def _rect(spec):
    return tuple(int(v) for v in spec.split(','))


def _windows(specs):
    """Список `--window ИМЯ=x0,y0,x1,y1` в dict; имена сохраняют порядок."""
    if not specs:
        raise SystemExit('нужен хотя бы один --window ИМЯ=x0,y0,x1,y1')
    out = {}
    for spec in specs:
        name, sep, box = spec.partition('=')
        if not sep or not name or ',' not in box:
            raise SystemExit(f'--window {spec!r}: ждём ИМЯ=x0,y0,x1,y1')
        out[name] = _rect(box)
    return out


def _offset(spec):
    if spec in ('auto', 'none'):
        return 'auto' if spec == 'auto' else None
    parts = spec.split(',')
    if len(parts) != 2:
        raise SystemExit('--offset: auto | none | dx,dy')
    return tuple(int(v) for v in parts)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Линейные замеры кадра')
    ap.add_argument('command',
                    choices=['runs', 'bbox', 'rows', 'glyph', 'windows', 'diff'])
    ap.add_argument('path', nargs='+', help='картинка (diff — две: a b)')
    ap.add_argument('--axis', default='y', choices=['x', 'y'], help='runs')
    ap.add_argument('--pos', type=int, help='разрез поперёк; по центру')
    ap.add_argument('--tone',
                    help="white|black|chroma|#hex (runs по умолчанию white, "
                         "bbox — chroma)")
    ap.add_argument('--tol', type=int, help='допуск тона (runs) / остаток px (diff)')
    ap.add_argument('--min', type=int, default=IMAGE_SCAN_MIN_RUN, help='runs')
    ap.add_argument('--rect', help='x0,y0,x1,y1 зона поиска')
    ap.add_argument('--thresh', type=int, default=IMAGE_SCAN_THRESH)
    ap.add_argument('--alpha-thresh', type=int, default=0,
                    help='bbox: учитывать только пиксели с альфой выше порога '
                         '(128 — для RGBA-иконок)')
    ap.add_argument('--window', action='append', metavar='ИМЯ=x0,y0,x1,y1',
                    help='окно для windows/diff, повторять по числу точек')
    ap.add_argument('--offset', default='auto',
                    help="diff: auto — медиана смещения, none — без, либо dx,dy")
    ns = ap.parse_args()
    try:
        rect = _rect(ns.rect) if ns.rect else None
        if ns.command == 'runs':
            tol = ns.tol if ns.tol is not None else IMAGE_SCAN_TOL
            r = image_scan_runs(ns.path[0], axis=ns.axis, pos=ns.pos,
                                tone=ns.tone or 'white',
                                tol=tol, min_len=ns.min, rect=rect)
            print(f"разрез {ns.axis}={r['pos']}, тон {r['tone']}: {r['count']}")
            for a, b in r['runs']:
                print(f'  {a}..{b}  (len {b - a + 1})')
        elif ns.command == 'bbox':
            tol = ns.tol if ns.tol is not None else IMAGE_SCAN_TOL
            r = image_scan_bbox(ns.path[0], tone=ns.tone or 'chroma',
                                tol=tol, rect=rect, alpha_thresh=ns.alpha_thresh)
            if r['box'] is None:
                print(f"тон {r['tone']}: чисто, пикселей 0")
            else:
                x0, y0, x1, y1 = r['box']
                print(f"габарит {x0},{y0} — {x1},{y1} "
                      f"(тон {r['tone']}, пикселей {r['count']}, доля канвы "
                      f"ш {r['share_w']:.3f} в {r['share_h']:.3f})")
        elif ns.command == 'rows':
            r = image_scan_rows(ns.path[0], rect=rect, thresh=ns.thresh)
            print(f"строк {len(r['rows'])}, шаг {r['pitch']}")
            for (a, b), c in zip(r['rows'], r['centers']):
                print(f'  {a}..{b}  центр {c}')
        elif ns.command == 'glyph':
            if not rect:
                raise SystemExit('glyph требует --rect x0,y0,x1,y1')
            r = image_scan_glyph(ns.path[0], rect, thresh=ns.thresh)
            print(f"габарит {r['x0']},{r['y0']} — {r['x1']},{r['y1']} "
                  f"(w {r['w']} h {r['h']}, пикселей {r['count']})")
        elif ns.command == 'windows':
            r = image_scan_windows(ns.path[0], _windows(ns.window),
                                   thresh=ns.thresh)
            for name, box in r['windows'].items():
                if box is None:
                    print(f'{name}\tпусто')
                else:
                    print(f"{name}\t{box['x0']},{box['y0']} — {box['x1']},{box['y1']}"
                          f"  (w {box['w']} h {box['h']}, пикселей {box['count']})")
        else:
            if len(ns.path) != 2:
                raise SystemExit('diff требует две картинки: a и b')
            tol = ns.tol if ns.tol is not None else IMAGE_SCAN_DIFF_TOL
            r = image_scan_windows_diff(ns.path[0], ns.path[1], _windows(ns.window),
                                        offset=_offset(ns.offset),
                                        thresh=ns.thresh, tol=tol)
            off = 'не найдено' if r['offset'] is None else tuple(r['offset'])
            verdict = ('сходится' if r['ok']
                       else 'расходится — ' + ', '.join(r['bad']))
            print(f"смещение {off}, допуск {r['tol']} px, вердикт: {verdict}")
            for name, e in r['windows'].items():
                resid = '—' if e['resid'] is None else tuple(e['resid'])
                print(f"{name}\tостаток {resid}\t{'ok' if e['ok'] else 'БОЛЬНО'}")
    except (ValueError, FileNotFoundError) as err:
        raise SystemExit(f'ошибка: {err}')

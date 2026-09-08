"""
Линейные замеры кадра: однотонные отрезки по разрезу, строки текста с шагом,
габарит глифов в окне.

Инструменты «где граница» для сверки свёрстанного со снимком. По одиночной
вертикали/горизонтали кадра: где начинается и заканчивается белая панель
(`image_scan_runs` — список однотонных отрезков с их концами), с каким шагом
идут строки текста и где их центры (`image_scan_rows` — профиль тёмных
строк, по нему же сверяют pitch меню), какой высоты реально отрисовались
глифы в окне (`image_scan_glyph` — капитель/ascender, по нему подбирают
`font-size` под замер с телефона: «капитель 38 px — это 4.81 vw»).

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


def image_scan_runs(path, axis='y', pos=None, tone='white',
                    tol=IMAGE_SCAN_TOL, min_len=IMAGE_SCAN_MIN_RUN,
                    rect=None) -> dict:
    """
    Однотонные отрезки вдоль вертикального или горизонтального разреза кадра.

    Так ищут края панелей по одной линии: «белый лист начинается тут,
    кончается там, ниже — щель, потом пилюля». `tone` — 'white', 'black'
    или hex («#EDEDED») с допуском `tol` по каждому каналу.

    Args:
        path: картинка.
        axis: 'y' — разрез-колонка (отрезки по вертикали), 'x' — разрез-строка.
        pos: координата разреза поперёк (для 'y' — x колонки);
            пусто — центр кадра (у краёв врут скруглённые углы панелей).
        tone: 'white' | 'black' | '#rrggbb'.
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


def _tone_mask(line, tone, tol):
    """Булева «линия попадает в тон» по всем пикселям строки/колонки."""
    if tone == 'white':
        return line.min(axis=1) >= 255 - tol
    if tone == 'black':
        return line.max(axis=1) <= tol
    if len(tone) == 7 and tone[0] == '#':
        rgb = np.array([int(tone[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.int32)
        return (np.abs(line - rgb) <= tol).all(axis=1)
    raise ValueError(f"tone — 'white', 'black' или '#rrggbb', получено {tone!r}")


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


def _rect(spec):
    return tuple(int(v) for v in spec.split(','))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Линейные замеры кадра')
    ap.add_argument('command', choices=['runs', 'rows', 'glyph'])
    ap.add_argument('path', help='картинка')
    ap.add_argument('--axis', default='y', choices=['x', 'y'], help='runs')
    ap.add_argument('--pos', type=int, help='разрез поперёк; по центру')
    ap.add_argument('--tone', default='white', help="runs: white|black|#hex")
    ap.add_argument('--tol', type=int, default=IMAGE_SCAN_TOL)
    ap.add_argument('--min', type=int, default=IMAGE_SCAN_MIN_RUN, help='runs')
    ap.add_argument('--rect', help='x0,y0,x1,y1 зона поиска')
    ap.add_argument('--thresh', type=int, default=IMAGE_SCAN_THRESH)
    ns = ap.parse_args()
    try:
        rect = _rect(ns.rect) if ns.rect else None
        if ns.command == 'runs':
            r = image_scan_runs(ns.path, axis=ns.axis, pos=ns.pos, tone=ns.tone,
                                tol=ns.tol, min_len=ns.min, rect=rect)
            print(f"разрез {ns.axis}={r['pos']}, тон {r['tone']}: {r['count']}")
            for a, b in r['runs']:
                print(f'  {a}..{b}  (len {b - a + 1})')
        elif ns.command == 'rows':
            r = image_scan_rows(ns.path, rect=rect, thresh=ns.thresh)
            print(f"строк {len(r['rows'])}, шаг {r['pitch']}")
            for (a, b), c in zip(r['rows'], r['centers']):
                print(f'  {a}..{b}  центр {c}')
        else:
            if not rect:
                raise SystemExit('glyph требует --rect x0,y0,x1,y1')
            r = image_scan_glyph(ns.path, rect, thresh=ns.thresh)
            print(f"габарит {r['x0']},{r['y0']} — {r['x1']},{r['y1']} "
                  f"(w {r['w']} h {r['h']}, пикселей {r['count']})")
    except (ValueError, FileNotFoundError) as err:
        raise SystemExit(f'ошибка: {err}')

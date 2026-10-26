"""Полосы-локаторы: где в длинном кадре лежит нужный цвет.

Кадр длинной страницы — два с половиной метра вертикали — ищет глазами:
«покажи заголовок блока». Цветная плашка (beige-шапка таблицы, серая
полоса разделителя) задаёт границу нужной зоны, но найти её пиксельным
сканом каждый раз — одна и та же строчка с range и step. Здесь: скан
ширины с шагом, порог доли совпавших пикселей, склейка соседних строк в
полосы — и сразу кроп каждой полосы в файл, чтобы смотреть, а не гадать.

## Что здесь есть

* `image_band_find` — горизонтальные полосы заданного цвета: [(y0, y1)];
* `image_band_crop` — те же полосы, накропанные в файлы (с отступом).

⚠ Полоса — это цвет, а не смысл: та же плашка может быть и у чужого
блока; назначение проверяют кадром, не координатой.
"""
from PIL import Image


def image_band_find(src, color, *, tol=(6, 6, 6), ratio: float = 0.75,
                    x_step: int = 40, y_step: int = 1, pad: int = 0,
                    min_height: int = 2) -> list:
    """Горизонтальные полосы строк, где цвет заполняет ширину.

    Args:
        src: путь или PIL.Image.
        color: эталон, (r, g, b) 0..255.
        tol: допуск по каналам, (dr, dg, db).
        ratio: доля точек строки (с шагом x_step), нужная для «полосы».
        x_step, y_step: шаг сэмплирования, пикселей.
        pad: расширение полосы вверх/вниз в результате.
        min_height: минимальная высота полосы, чтобы не ловить шум.

    Returns:
        [(y0, y1)] — в порядке сверху вниз, y1 включительно за вычетом pad.
    """
    im = src if hasattr(src, 'load') else Image.open(src)
    im = im.convert('RGB')
    w, h = im.size
    px = im.load()
    hits = []
    for y in range(0, h, y_step):
        good = total = 0
        for x in range(0, w, x_step):
            p = px[x, y]
            total += 1
            if all(abs(p[i] - color[i]) <= tol[i] for i in range(3)):
                good += 1
        if total and good / total >= ratio:
            hits.append(y)
    bands, start, prev = [], None, None
    for y in hits:
        if start is None:
            start = prev = y
        elif y - prev <= y_step * 2:
            prev = y
        else:
            bands.append((start, prev))
            start = prev = y
    if start is not None:
        bands.append((start, prev))
    return [(max(0, a - pad), min(h - 1, b + pad))
            for a, b in bands if b - a + 1 >= min_height]


def image_band_crop(src, out_prefix, *, fmt: str = 'png', ext: str = '',
                    **kw) -> dict:
    """Каждая полоса — в файл `out_prefix_01.png`…; путь → список файлов.

    Args:
        src: путь или PIL.Image.
        out_prefix: куда и как кропать (`/tmp/t8` → `/tmp/t8_01.png`).
        fmt: формат PIL.
        ext: расширение (по умолчанию по fmt).
        kw: что передать `image_band_find` (color, tol, ratio, pad…).

    Returns:
        {'ok': bool, 'bands': [(y0,y1)], 'files': [пути]}
    """
    im = src if hasattr(src, 'load') else Image.open(src)
    bands = image_band_find(im, **kw)
    files = []
    for n, (a, b) in enumerate(bands, 1):
        path = f'{out_prefix}_{n:02d}.{ext or fmt}'
        im.crop((0, a, im.width, b + 1)).save(path, format=fmt.upper())
        files.append(path)
    return {'ok': bool(files), 'bands': bands, 'files': files}


if __name__ == '__main__':
    import argparse
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('src')
    _P.add_argument('color', help='эталон hex, напр. f0ead8')
    _P.add_argument('--tol', default='6,6,6')
    _P.add_argument('--ratio', type=float, default=0.75)
    _P.add_argument('--pad', type=int, default=0)
    _P.add_argument('--out', default='', help='префикс для кропов')
    _a = _P.parse_args()
    col = tuple(int(_a.color[i:i + 2], 16) for i in (0, 2, 4))
    tol = tuple(int(v) for v in _a.tol.split(','))
    if _a.out:
        res = image_band_crop(_a.src, _a.out, color=col, tol=tol,
                              ratio=_a.ratio, pad=_a.pad)
        print(*res['files'], sep='\n')
    else:
        for a, b in image_band_find(_a.src, col, tol=tol, ratio=_a.ratio,
                                    pad=_a.pad):
            print(f'{a}-{b}')

"""
Пара кадров: где именно отличаются и как вшить полосу из второго кадра.

Две стороны одной работы с артефактами, поверх которых правили руками:

`image_pair_diff_rows` отвечает на «чем эти два кадра различаются, строка за
строкой»: сгруппированные интервалы строк (или столбцов) с максимумом
различий выше порога, число пикселей и границы островков. Прежде чем
перезаписать артефакт перегенерацией, так ищут, где лежат ручные правки —
`image_diff_zones` с её сеткой 3×3 для этого слишком крупна: правка в 10
пикселей тонет в «зоне не изменилась».

`image_pair_splice` — латание без перегенерации: когда верх артефакта трогать
нельзя (ручные правки), а тон нижней части вышел неверным, вычищенные строки
из новой генерации вшиваются в разложенный файл от `pos` и дальше по оси.
Формат и режим сохраняются, webp переписывается с теми же параметрами, что
конвейер (quality/method).
"""
import numpy as np
from PIL import Image

IMAGE_PAIR_THRESH = 6      # пород «строка отличается», по каналу; шум кодирования ниже
IMAGE_PAIR_WEBP_QUALITY = 90
IMAGE_PAIR_WEBP_METHOD = 6


def image_pair_diff_rows(path_a, path_b, thresh=IMAGE_PAIR_THRESH,
                         axis='y', rect=None) -> dict:
    """
    Построчная (постолбцовая) сверка двух кадров одного размера.

    Строка считается отличающейся, если максимум |разности| по всем каналам
    и всем пикселям строки (в окне) превышает `thresh`. Идущие подряд
    отличающиеся строки слипаются в один интервал.

    Args:
        path_a, path_b: кадры; размеры и режимы должны совпадать.
        thresh: порог по каналу, 0-255.
        axis: 'y' — группировать строки, 'x' — столбцы.
        rect: (x0, y0, x1, y1) — окно сверки; пусто — весь кадр.

    Returns:
        {'file_a', 'file_b', 'axis', 'thresh', 'pixels': сколько пикселей
         окна отличаются, 'groups': [(начало, конец)] включительно вдоль оси,
         'bbox': (x0, y0, x1, y1) островков или None, 'max': максимальная
         разность по каналу}.

    Raises:
        ValueError: размеры или режимы кадров не совпали; rect вне кадра.
    """
    if axis not in ('x', 'y'):
        raise ValueError("axis — 'y' (строки) или 'x' (столбцы)")
    ia, ib = Image.open(path_a), Image.open(path_b)
    if ia.size != ib.size or ia.mode != ib.mode:
        raise ValueError(f'кадры не пара: {ia.size} {ia.mode} vs {ib.size} {ib.mode}')
    a = np.asarray(ia.convert('RGB'), dtype=np.int32)
    b = np.asarray(ib.convert('RGB'), dtype=np.int32)
    h, w = a.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in rect) if rect else (0, 0, w, h)
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError(f'rect {tuple(rect)!r} не лежит в кадре {w}x{h}')
    diff = np.abs(a - b)[y0:y1, x0:x1, :].max(axis=2)
    differs = diff.max(axis=1 if axis == 'y' else 0) > thresh
    groups = _runs(differs, y0 if axis == 'y' else x0)
    ys, xs = np.where(diff > thresh)
    bbox = None
    if ys.size:
        bbox = (int(xs.min()) + x0, int(ys.min()) + y0,
                int(xs.max()) + 1 + x0, int(ys.max()) + 1 + y0)
    return {'file_a': path_a, 'file_b': path_b, 'axis': axis, 'thresh': thresh,
            'pixels': int((diff > thresh).sum()), 'groups': groups, 'bbox': bbox,
            'max': int(diff.max()) if diff.size else 0}


def image_pair_splice(base, patch, out, pos, axis='y',
                      quality=IMAGE_PAIR_WEBP_QUALITY,
                      method=IMAGE_PAIR_WEBP_METHOD) -> dict:
    """
    Вшивка: в `base` часть от `pos` до конца оси заменяется частью `patch`.

    Кадры — строго пара (размер и режим), это не композитинг: так пересобирают
    артефакт, у которого верх правлен руками, а низ вышел из конвейера
    заново. Формат выхода — по расширению `out`: webp переписывается с
    `quality`/`method` (как конвейер), jpg — с `quality`, остальное — как
    умеет Pillow по расширению. Альфа переезжает как есть.

    Args:
        base, patch: кадры-пара (одинаковые размер и режим).
        out: куда писать; может совпадать с base (правит на месте).
        pos: координата границы вшивки вдоль оси, строки/столбцы от pos
            берутся из patch.
        axis: 'y' — горизонтальный шов (строки), 'x' — вертикальный.
        quality, method: параметры webp/jpeg.

    Returns:
        {'base', 'patch', 'out', 'axis', 'pos', 'size': [w, h], 'mode'}.

    Raises:
        ValueError: кадры не пара; pos вне оси.
    """
    if axis not in ('x', 'y'):
        raise ValueError("axis — 'y' (строки) или 'x' (столбцы)")
    ibase, ipatch = Image.open(base), Image.open(patch)
    if ibase.size != ipatch.size or ibase.mode != ipatch.mode:
        raise ValueError(f'base и patch не пара: {ibase.size} {ibase.mode} '
                         f'vs {ipatch.size} {ipatch.mode}')
    w, h = ibase.size
    along = h if axis == 'y' else w
    pos = int(pos)
    if not 0 <= pos <= along:
        raise ValueError(f'pos {pos} вне оси {axis}={along}')
    comp = ibase.copy()
    if axis == 'y':
        comp.paste(ipatch.crop((0, pos, w, h)), (0, pos))
    else:
        comp.paste(ipatch.crop((pos, 0, w, h)), (pos, 0))
    suffix = str(out).lower().rsplit('.', 1)[-1]
    kwargs = {'quality': quality, 'method': method} if suffix == 'webp' \
        else {'quality': quality} if suffix in ('jpg', 'jpeg') else {}
    comp.save(out, **kwargs)
    return {'base': base, 'patch': patch, 'out': str(out), 'axis': axis,
            'pos': pos, 'size': [w, h], 'mode': comp.mode}


def _runs(flags, start: int) -> list:
    """Слипшиеся True в интервалы (включительно, со сдвигом start)."""
    groups, s = [], None
    for i, f in enumerate(flags):
        if f and s is None:
            s = i
        elif not f and s is not None:
            groups.append((start + s, start + i - 1))
            s = None
    if s is not None:
        groups.append((start + s, start + len(flags) - 1))
    return groups


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description='Сверка пары кадров построчно и вшивка полосы из второго кадра.')
    ap.add_argument('command', choices=['diff-rows', 'splice'])
    ap.add_argument('files', nargs='+',
                    help='diff-rows: a и b; splice: base и patch')
    ap.add_argument('--thresh', type=int, default=IMAGE_PAIR_THRESH,
                    help='diff-rows: порог «строка отличается», по каналу')
    ap.add_argument('--axis', default='y', choices=['x', 'y'],
                    help='ось: y — строки, x — столбцы')
    ap.add_argument('--rect', default='', help='diff-rows: окно x0,y0,x1,y1')
    ap.add_argument('--out', default='', help='splice: куда писать')
    ap.add_argument('--pos', type=int, default=None,
                    help='splice: от какой координаты берутся строки patch')
    ap.add_argument('--quality', type=int, default=IMAGE_PAIR_WEBP_QUALITY,
                    help='splice: качество webp/jpeg')
    ap.add_argument('--method', type=int, default=IMAGE_PAIR_WEBP_METHOD,
                    help='splice: метод кодирования webp')
    ns = ap.parse_args()
    rect = tuple(int(v) for v in ns.rect.split(',')) if ns.rect else None

    try:
        if ns.command == 'diff-rows':
            if len(ns.files) != 2:
                raise SystemExit('нужно ровно два кадра')
            d = image_pair_diff_rows(ns.files[0], ns.files[1], thresh=ns.thresh,
                                     axis=ns.axis, rect=rect)
            what = 'строки' if ns.axis == 'y' else 'столбцы'
            print(f'{ns.files[0]} vs {ns.files[1]}: пикселей {d["pixels"]}, '
                  f'макс {d["max"]}, групп {len(d["groups"])}')
            for g0, g1 in d['groups']:
                print(f'  {what} {g0}..{g1}')
            if d['bbox']:
                print(f'  островки: {d["bbox"]}')
        else:
            if len(ns.files) != 2 or not ns.out or ns.pos is None:
                raise SystemExit('splice: нужны base, patch, --out и --pos')
            r = image_pair_splice(ns.files[0], ns.files[1], ns.out, ns.pos,
                                  axis=ns.axis, quality=ns.quality, method=ns.method)
            print(f'{ns.files[0]} + {ns.files[1]} от pos {ns.pos} ({ns.axis}) '
                  f'→ {r["out"]} ({r["size"][0]}x{r["size"][1]} {r["mode"]})')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')


if __name__ == '__main__':
    main()

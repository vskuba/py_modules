"""
Изображения: замер яркости подложки и тоновая подгонка по эталону.

Отвечает на два вопроса, которыми сверяют пачку картинок между собой:
«насколько ярким вышел фон» (`image_measure`) и «сделай так же, как на
эталоне» (`image_match`). Фон ищется модой — самой частной краской окна:
текста в окне заведомо меньше, чем подложки, а среднее по кадру уезжает
вперемешку с белыми карточками.

Правка — кусочно-линейные уровни (levels): точка фона переезжает на
целевую яркость, чёрный закреплён в нуле, белый — в 255. Контраст тёмного
текста по фону при этом сохраняется, меняется только «прогрев» подложки.
Профиль идемпотентен лишь относительно оригинала, поэтому `image_match`
с `backup` правит всегда от нетронутой копии: повторный прогон повторяет
первый, а не складывает яркость дважды. Грабли замера — `image_tooling.md`.
"""
from collections import Counter
from pathlib import Path

from PIL import Image

# Pillow по умолчанию пишет JPEG с качеством 75 — на цельноэкранной графике
# это заметные артефакты вокруг текста; для правки фонов хватает 95.
IMAGE_JPEG_QUALITY = 95


def image_measure(path: str, box: tuple = None) -> dict:
    """
    Замерить фон картинки: моду цвета и её яркость.

    Args:
        path: файл картинки.
        box: окно замера `(x0, y0, x1, y1)` долями кадра, чтобы не зависеть
            от размера; пусто — весь кадр. Полоской у верхнего края
            (например, `(0.55, 0.015, 0.95, 0.06)`) берут полосу, где
            заведомо нет текста заголовка.

    Returns:
        {'mode': (r,g,b) самой частой краски, 'luma': её яркость 0–255,
         'mean_luma': средняя по окну, 'samples': число замеренных пикселей}.
    """
    pixels = _window(_load(path), box)
    mode = Counter(pixels).most_common(1)[0][0]
    return {'mode': mode, 'luma': _luma(mode),
            'mean_luma': round(sum(_luma(p) for p in pixels) / len(pixels)),
            'samples': len(pixels)}


def image_match(path: str, target, out: str = '', backup: str = '',
                box: tuple = None) -> dict:
    """
    Подогнать яркость фона картинки под эталон кусочно-линейными уровнями.

    Точка фона переезжает на яркость эталона; чёрный и белый закреплены,
    поэтому текст не сливается с фоном, а света не клиппуется.

    Args:
        path: правая картинка.
        target: эталон — путь к картинке (целью станет яркость её фона)
            или готовая яркость 0–255 числом.
        out: куда писать; пусто — поверх `path`.
        backup: каталог нетронутых оригиналов; правка идёт от них, а не от
            текущего состояния файла. Нет копии — она заводится первым
            делом; второй прогон тогда повторяет первый, а не складывает
            яркость дважды.
        box: окно замера фона, как в `image_measure`.

    Returns:
        {'file': куда записано, 'before': яркость фона до, 'after': после
         (пересчитана с записанного файла), 'target': целевая яркость,
         'changed': был ли записан файл}.
    """
    src = Path(path)
    if backup:
        src = Path(backup) / Path(path).name
        if not src.exists():
            Path(backup).mkdir(parents=True, exist_ok=True)
            src.write_bytes(Path(path).read_bytes())
    dst = Path(out) if out else Path(path)

    want = target if isinstance(target, int) else image_measure(str(target), box=box)['luma']
    got = image_measure(str(src), box=box)['luma']
    if abs(got - want) <= 1 or not 0 < got < 255:
        return {'file': str(dst), 'before': got, 'after': got,
                'target': want, 'changed': False}

    image = _load(str(src))
    image = image.point(_levels_lut(got, want))
    if dst.suffix.lower() in ('.jpg', '.jpeg'):
        image.save(dst, quality=IMAGE_JPEG_QUALITY)
    else:
        image.save(dst)
    after = image_measure(str(dst), box=box)
    return {'file': str(dst), 'before': got, 'after': after['luma'],
            'target': want, 'changed': True}


def _load(path: str) -> Image.Image:
    """Картинка в RGB: режим P и альфа снимкам экрана не нужны, а в LUT лягут криво."""
    return Image.open(path).convert('RGB')


def _window(img: Image.Image, box: tuple = None) -> list:
    """Пиксели окна (доли кадра) списком RGB-кортежей."""
    if box:
        w, h = img.size
        x0, y0, x1, y1 = box
        img = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    # Pillow 14 объявил getdata устаревшим в пользу get_flattened_data,
    # а в старых версиях нового имени нет — берём то, что есть.
    if hasattr(img, 'get_flattened_data'):
        return list(img.get_flattened_data())
    return list(img.getdata())


def _levels_lut(bg: int, target: int) -> tuple:
    """
    Палитра уровней для `Image.point`: якоря 0 → 0, фон → цель, 255 → 255,
    одна и та же кривая на каждый канал. `Image.point` на RGB просит ровно
    768 записей — 256, повторённые трижды, иначе «wrong number of lut
    entries».
    """
    low = [round(v * target / bg) for v in range(bg)]
    high = [round(target + (v - bg) * (255 - target) / (255 - bg)) for v in range(bg, 256)]
    return tuple((low + high) * 3)


def _luma(rgb: tuple) -> int:
    """Яркость Rec.601 — то же восприятие, что у глаз."""
    r, g, b = rgb
    return round(0.299 * r + 0.587 * g + 0.114 * b)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Замер яркости фона и тоновая подгонка картинок по эталону.')
    parser.add_argument('command', choices=['measure', 'match'])
    parser.add_argument('files', nargs='+', help='картинки')
    parser.add_argument('--target', default='', help='эталон для match: файл или яркость 0-255')
    parser.add_argument('--out', default='', help='каталог для match; пусто — поверх')
    parser.add_argument('--backup', default='', help='каталог нетронутых оригиналов (идемпотентность)')
    parser.add_argument('--box', default='', help='окно замера x0,y0,x1,y1 долями кадра')
    ns = parser.parse_args()
    box = tuple(float(v) for v in ns.box.split(',')) if ns.box else None

    try:
        if ns.command == 'measure':
            for f in ns.files:
                m = image_measure(f, box=box)
                print(f'{f}: фон {m["mode"]} L={m["luma"]} (средняя {m["mean_luma"]}, '
                      f'{m["samples"]} px)')
        else:
            if not ns.target:
                raise SystemExit('нужен --target: файл эталона или яркость числом')
            target = int(ns.target) if ns.target.isdigit() else ns.target
            for f in ns.files:
                out = str(Path(ns.out) / Path(f).name) if ns.out else f
                r = image_match(f, target, out=out, backup=ns.backup, box=box)
                print(f'{f}: {r["before"]} → {r["after"]} (цель {r["target"]}, '
                      f'{"записано" if r["changed"] else "уже в допуске"}) → {r["file"]}')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

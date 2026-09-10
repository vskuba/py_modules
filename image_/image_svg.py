"""
SVG в PNG точного размера: растр ImageMagick-ом с суперсэмплингом.

Растеризаторы SVG капризны не в одинаковости результата, а в деталях: часть
из них рисует «дырки» глифов — counters букв Д, Я, B — на сторону штриха, и
буква зарастает. ImageMagick печатает counters правильно и стоит в системе,
когда cairosvg/rsvg/inkscape не ставились вовсе; это единственная ниточка из
вектора (например, pathData, вынутых `apk_.apk_` из чужого APK) в растр,
который меряют `image_.image_scan`.

Кривую нельзя рисовать сразу в мелкую сетку: точки попадает между пикселями и
край ступенится. Поэтому растр берётся в `supersample` раз крупнее и ужимается
LANCZOS-ом — так размер на выходе точный, а край гладкий.
"""
import argparse
import os
import re
import shutil
import subprocess
import tempfile

# Корневой тег svg: атрибуты могут стоять на нескольких строках — DOTALL.
IMAGE_SVG_ROOT_RE = re.compile(r'<svg\b[^>]*>', re.DOTALL)

# width/height корня: снимаем перед установкой своего размера.
IMAGE_SVG_SIDE_RE = re.compile(r'\s(?:width|height)\s*=\s*"[^"]*"')

# Числовой width/height (без %, em и прочего) — из них синтезируется viewBox.
IMAGE_SVG_NUM_RE = re.compile(r'\b(width|height)\s*=\s*"(\d+(?:\.\d+)?)"')


def image_svg_render(svg, out: str = '', size=512, supersample: int = 3) -> str:
    """
    Наложить SVG (файл или разметку строкой) в PNG заданного размера с прозрачностью.

    Размер ставится атрибутом на корень svg, поэтому масштабируется всё
    содержимое: если viewBox в разметке нет, он синтезируется из числовых
    width/height оригинала. Фон — прозрачный (`-background none`), альфа
    доживает до выходного PNG: так иконку видно и на светлом, и на тёмном.

    Args:
        svg: путь к .svg или сама разметка (начинается с `<`).
        out: куда положить png; пусто — рядом с исходником `то же .png`,
            для строки-разметки — `/tmp/image_svg.png`.
        size: сторона (или пара `(w, h)`) выходного PNG в пикселях.
        supersample: во сколько раз крупнее брать растр перед ужимкой LANCZOS;
            1 = без суперсэмплинга (край ступенчатый).

    Returns:
        Путь записанного png.

    Raises:
        FileNotFoundError: ImageMagick не установлен или файла-исходника нет.
        ValueError: разметка без viewBox и без числовых размеров — нечем
            масштабировать содержимое.
    """
    from PIL import Image      # тяжёлый стек — лениво, как в остальных модулях

    if isinstance(size, (tuple, list)):
        w, h = (int(v) for v in size)
    else:
        w = h = int(size)
    if not out:
        out = (os.path.splitext(svg)[0] + '.png' if _is_file(svg)
               else '/tmp/image_svg.png')
    text = open(svg, encoding='utf-8').read() if _is_file(svg) else str(svg)

    ss = max(1, int(supersample))
    tmp_dir = tempfile.mkdtemp(prefix='image_svg-')
    try:
        big = _with_size(text, w * ss, h * ss)
        src = os.path.join(tmp_dir, 'in.svg')
        raster = os.path.join(tmp_dir, 'big.png')
        with open(src, 'w', encoding='utf-8') as f:
            f.write(big)
        done = subprocess.run([_magick(), '-background', 'none', src, raster],
                              capture_output=True, text=True, timeout=120)
        if done.returncode != 0 or not os.path.exists(raster):
            raise RuntimeError(f'ImageMagick не rasterизовал svg: '
                               f'{(done.stderr or done.stdout).strip()[-300:]}')
        os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
        Image.open(raster).convert('RGBA').resize((w, h), Image.LANCZOS).save(out)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return out


def _magick() -> str:
    """
    ImageMagick: v7 `magick`, иначе v6 `convert`; чужая одноимённая утилита — не он.

    `convert` — имя, которое на части систем занимает утилита ext2fs (обычно
    вне PATH, но не всегда), поэтому версия проверяется по слову ImageMagick,
    а не фактом нахождения бинарника. v7 предпочтительнее: у v6 внутренний
    MSVG-рендер хуже рисует пути, если не собран с librsvg.
    """
    for name in ('magick', 'convert'):
        path = shutil.which(name)
        if not path:
            continue
        done = subprocess.run([path, '-version'], capture_output=True,
                              text=True, timeout=30)
        if 'ImageMagick' in done.stdout:
            return path
    raise FileNotFoundError(
        'ImageMagick (magick или convert) не найден в PATH — без него svg в '
        'растр не превратить; колесом в pip он не ставится, это системный пакет')


def _with_size(text: str, w: int, h: int) -> str:
    """
    Разметка с width/height корня = w/h пикселей; viewBox приводится в порядок.

    Нет viewBox — синтезируется из числовых width/height оригинала: без него
    атрибут размера расширяет только холст, содержимое остаётся в углу. Если
    исходник без viewBox и с процентами в размерах — масштабировать не от
    чего, это ValueError, а не тихая лужа в углу кадра.
    """
    root = IMAGE_SVG_ROOT_RE.search(text)
    if not root:
        raise ValueError('в разметке нет корневого тега <svg>')
    tag = root.group(0)
    if 'viewBox' not in tag:
        nums = dict(IMAGE_SVG_NUM_RE.findall(tag))
        if not {'width', 'height'} <= set(nums):
            raise ValueError('svg без viewBox и без числовых width/height — '
                             'нечем масштабировать содержимое')
        tag = tag[:4] + f' viewBox="0 0 {nums["width"]} {nums["height"]}"' + tag[4:]
    tag = IMAGE_SVG_SIDE_RE.sub('', tag)
    tag = tag[:4] + f' width="{w}" height="{h}"' + tag[4:]
    return text[:root.start()] + tag + text[root.end():]


def _is_file(svg) -> bool:
    """Похож ли аргумент на существующий путь к файлу, а не на строку разметки."""
    return isinstance(svg, str) and not svg.lstrip().startswith('<') \
        and os.path.isfile(svg)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='SVG в PNG точного размера: ImageMagick + суперсэмплинг, '
                    'прозрачный фон.')
    ap.add_argument('svg', help='путь к .svg')
    ap.add_argument('-o', '--out', default='', help='куда положить png '
                    '(пусто — рядом с исходником)')
    ap.add_argument('--size', type=int, default=512, help='сторона png, px')
    ap.add_argument('--supersample', type=int, default=3,
                    help='увеличение растра перед ужимкой LANCZOS')
    ns = ap.parse_args()
    try:
        print(image_svg_render(ns.svg, out=ns.out, size=ns.size,
                               supersample=ns.supersample))
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

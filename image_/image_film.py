"""Плёнка из кадров: что изменилось между ними и GIF по порядку.

Сценарий в браузере делает десятки шагов, и половина из них ничего на экране
не меняет — паузы, чтения, вычисления. Снимок после каждого даёт ленту, где
одинаковые кадры идут подряд пачками: смотреть её бесполезно, хранить дорого.

Модуль отвечает на два вопроса: **«изменилось ли что-нибудь»** и **«собери из
этого плёнку»**. Вместе они дают запись работы, где каждый кадр — настоящее
изменение, а не отсчёт времени.

```python
from image_.image_film import image_film_same, image_film_gif

if not image_film_same(previous, shot):      # что-то поменялось
    film.append(shot)

image_film_gif(film, 'run.gif', ms=900)
```

## ⚠⚠ Сравнение перцептивное, а не побайтовое

Два снимка неподвижной страницы **не равны по байтам**: JPEG пересобирается
каждый раз, а в кадр попадают мигающий курсор, подгруженный шрифт, сдвиг
анимации на пиксель. Побайтовое сравнение объявит изменением каждый кадр, и
плёнка перестанет отличаться от ленты «снимай всё подряд».

Здесь сравниваются **dHash** уменьшенных кадров: знак перепада яркости между
соседними точками. Он не замечает общего осветления и шума сжатия, но ловит
любую перерисовку — появившийся блок, заполненное поле, раскрытый шаг.

## ⚠ Порог — в битах, и он не «чем меньше, тем лучше»

`IMAGE_FILM_TOLERANCE` — сколько битов хеша могут разойтись у кадров, которые
мы считаем одинаковыми. Ноль ловил бы дрожание шрифтов; большое значение
съедало бы настоящие изменения вроде одной заполненной строки.

## ⚠⚠ Pillow, а не OpenCV

`image_unique` считает те же хеши точнее и тремя способами, но тянет `cv2` —
десятки мегабайт в образ ради одного вопроса «одинаковы ли два кадра».
Здесь хватает Pillow, который в слое и так есть.
"""

import argparse
import io
import pathlib
import sys

# Сторона уменьшенного кадра для хеша. ⚠ Восемь на девять: dHash сравнивает
# соседей по горизонтали, и ему нужен лишний столбец.
IMAGE_FILM_SIDE = 8

# Сколько битов хеша могут разойтись у «одинаковых» кадров. См. шапку.
IMAGE_FILM_TOLERANCE = 2

# Сколько миллисекунд держать кадр в GIF по умолчанию.
#
# ⚠ Почти секунда, а не кинематографические 40 мс: это не кино, а разбор —
# каждый кадр читают, а не смотрят. Быстрее человек не успевает понять, что
# изменилось.
IMAGE_FILM_FRAME_MS = 900

# Сколько держать ПОСЛЕДНИЙ кадр. ⚠ Дольше прочих: на нём итог работы, и без
# паузы GIF возвращается к началу ровно в тот момент, когда на него посмотрели.
IMAGE_FILM_LAST_MS = 2500

# Ширина кадра в плёнке. ⚠⚠ Не исходная: кадр страницы — 1280 точек и сотня
# килобайт, а GIF из сорока таких весит десятки мегабайт и не открывается
# вовсе. Шестьсот точек читаются на экране и весят вдесятеро меньше.
IMAGE_FILM_WIDTH = 600

# Потолок высоты кадра в плёнке.
#
# ⚠⚠ Полная страница бывает в три экрана, и вписанная целиком она становится
# нечитаемой. Берём верх — то место, где и происходит работа: формы
# заполняются сверху вниз, и низ страницы это подвал с контактами.
IMAGE_FILM_HEIGHT = 900


def image_film_same(before: bytes, after: bytes,
                    tolerance: int = IMAGE_FILM_TOLERANCE) -> bool:
    """Одинаковы ли два кадра на вид; `True` — ничего не изменилось.

    Args:
        before: кадр до шага, байты PNG или JPEG.
        after: кадр после.
        tolerance: сколько битов хеша прощаем (см. шапку).

    Returns:
        `True`, если кадры неотличимы на глаз. Пустой любой из них — `False`:
        «нечего сравнивать» это не «одинаковы», и терять кадр из-за сбоя
        снимка нельзя.

    ⚠ Не бросает на нечитаемом кадре: отвечает `False`. Снимок — свидетельство,
    и беда с одним кадром не должна рушить сбор остальных.

    >>> image_film_same(b'', b'')
    False
    """
    if not before or not after:
        return False

    try:
        return _hamming(_film_hash(before), _film_hash(after)) <= tolerance
    except Exception:  # noqa: BLE001 — кадр не стоит прогона
        return False


def image_film_gif(frames: list, out: str, ms: int = IMAGE_FILM_FRAME_MS,
                   width: int = IMAGE_FILM_WIDTH,
                   height: int = IMAGE_FILM_HEIGHT) -> dict:
    """Собрать GIF из кадров по порядку; вернуть, что получилось.

    Args:
        frames: кадры байтами, в порядке съёмки.
        out: куда записать `.gif`.
        ms: сколько держать кадр.
        width: до какой ширины ужимать.
        height: потолок высоты; выше — берётся верх кадра (см. шапку).

    Returns:
        `{'frames', 'bytes', 'path', 'error'}`. Кадров меньше двух — файл не
        пишется вовсе и приходит `error`: «плёнка» из одного кадра это просто
        кадр, и отдельный файл под него только путает.

    Raises:
        RuntimeError: нет Pillow.

    ⚠ Последний кадр держится дольше (`IMAGE_FILM_LAST_MS`): на нём итог, и
    без паузы плёнка возвращается к началу ровно тогда, когда на него
    посмотрели.
    """
    if len(frames or []) < 2:
        return {'frames': len(frames or []), 'bytes': 0, 'path': '',
                'error': 'кадров меньше двух — плёнки не выйдет'}

    image = _pillow()
    shots = []
    for raw in frames:
        try:
            shots.append(_film_fit(image.open(io.BytesIO(raw)), width, height))
        except Exception:  # noqa: BLE001 — битый кадр пропускаем
            continue

    if len(shots) < 2:
        return {'frames': len(shots), 'bytes': 0, 'path': '',
                'error': 'читаемых кадров меньше двух'}

    # ⚠ Длительность списком, а не числом: у последнего кадра она своя.
    hold = [ms] * (len(shots) - 1) + [IMAGE_FILM_LAST_MS]

    path = pathlib.Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    shots[0].save(path, save_all=True, append_images=shots[1:],
                  duration=hold, loop=0, optimize=True)

    return {'frames': len(shots), 'bytes': path.stat().st_size,
            'path': str(path), 'error': ''}


# ── детали реализации ──────────────────────────────────────────────────────

def _pillow():
    """Модуль `Image` Pillow; нет его — отказ словами, а не `ImportError`."""
    try:
        from PIL import Image
    except ImportError as fault:
        raise RuntimeError('для плёнки нужен Pillow: pip install Pillow') \
            from fault

    return Image


def _film_hash(raw: bytes) -> int:
    """dHash кадра числом: знак перепада яркости между соседями.

    ⚠ Именно перепад, а не сами значения: так хеш не замечает общего
    осветления и шума сжатия, но ловит перерисовку — ради этого он и взят.
    """
    image = _pillow()
    small = image.open(io.BytesIO(raw)).convert('L').resize(
        (IMAGE_FILM_SIDE + 1, IMAGE_FILM_SIDE))
    dots = list(small.getdata())

    bits = 0
    for row in range(IMAGE_FILM_SIDE):
        line = row * (IMAGE_FILM_SIDE + 1)
        for col in range(IMAGE_FILM_SIDE):
            bits <<= 1
            if dots[line + col] > dots[line + col + 1]:
                bits |= 1

    return bits


def _hamming(one: int, two: int) -> int:
    """Сколько битов разошлось у двух хешей."""
    return bin(one ^ two).count('1')


def _film_fit(shot, width: int, height: int):
    """Кадр, ужатый по ширине и обрезанный по верху, в режиме GIF.

    ⚠ Обрезка, а не вписывание: страница бывает в три экрана, и вписанная
    целиком она становится нечитаемой. Верх — то место, где идёт работа.

    ⚠ Перевод в палитру здесь, а не при записи: Pillow иначе считает её для
    первого кадра и применяет ко всем, и цвета остальных плывут.
    """
    image = _pillow()
    if shot.width > width:
        tall = max(1, round(shot.height * width / shot.width))
        shot = shot.resize((width, tall), image.LANCZOS)
    if shot.height > height:
        shot = shot.crop((0, 0, shot.width, height))

    return shot.convert('RGB').convert('P', palette=image.ADAPTIVE)


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog='python -m image_.image_film',
        description='Плёнка из кадров: GIF по порядку, без повторов.')
    parser.add_argument('shots', nargs='+', help='кадры по порядку')
    parser.add_argument('--out', default='film.gif', help='куда записать')
    parser.add_argument('--ms', type=int, default=IMAGE_FILM_FRAME_MS,
                        help='сколько держать кадр')
    parser.add_argument('--all', action='store_true',
                        help='не выбрасывать одинаковые подряд')
    args = parser.parse_args()

    film, last = [], b''
    for name in args.shots:
        raw = pathlib.Path(name).read_bytes()
        if args.all or not image_film_same(last, raw):
            film.append(raw)
            last = raw

    got = image_film_gif(film, args.out, args.ms)
    if got['error']:
        print(f'ошибка: {got["error"]}', file=sys.stderr)

        return 1

    print(f'{got["path"]}: кадров {got["frames"]}, '
          f'{got["bytes"] // 1024} КБ (из {len(args.shots)} снятых)')

    return 0


if __name__ == '__main__':
    raise SystemExit(_main())

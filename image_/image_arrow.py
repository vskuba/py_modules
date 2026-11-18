"""
Стрелки с подписями на скриншоте: рамка вокруг важного места и стрелка к ней.

Родился на задаче «покажи в тикете, где на скриншоте нужное»: строка каскада,
статус транзакции, номер выплаты. Рамка без стрелки теряется на плотной таблице,
стрелка без рамки — непонятно, на что указывает; здесь обе, плюс подпись на
плашке у хвоста стрелки.

Координаты — пиксели кадра `(x, y, w, h)`. Точнее всего брать их из браузера
(`locator.bounding_box()`), а не на глаз: `browser_login_shots --mark` так и делает.

## Куда ставится плашка

Не в фиксированную сторону, а **в самое пустое место рядом**: вокруг рамки
перебираются 8 направлений × 3 расстояния, у каждого кандидата считается
«занятость» — разброс яркости снимка под плашкой и вдоль линии стрелки (пустой
фон — ноль, текст и таблица — много). Кандидаты, налезающие на уже нарисованные
рамки и плашки или вылезающие за кадр, отбрасываются. Кончик стрелки всегда на
краю рамки, линия — от ближайшего края плашки.

⚠ Так было сделано после первой версии «хвост справа, не влезло — слева»: на
плотной таблице плашка ложилась прямо на значения, которые надо прочесть
(«1.00 USD», «Nuvei»), а метки на одной высоте разводились сдвигом вместе с
кончиком — стрелка указывала в пустоту. Рамки всех меток считаются занятыми
заранее, до первой плашки.

⚠ Метка на всю строку таблицы почти всегда хуже метки на ячейку; у поля формы
«Подпись: значение» — метка на строку целиком (`text=/Подпись/ >> xpath=..`),
иначе рамка обведёт одну подпись.

CLI:

    python -m image_.image_arrow shot.png out.png \\
        --mark '120,240,600,40=Nuvei в каскаде' --mark '300,500,80,30=Success'
"""
import argparse
import math

from PIL import Image, ImageDraw, ImageStat

from image_.image_ import image_font

ARROW_COLOR = '#e11d48'
ARROW_WIDTH = 5
ARROW_PAD = 6
ARROW_DISTANCES = (40, 110, 200)
ARROW_DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def image_arrows(path: str, marks: list, out: str = '', color: str = ARROW_COLOR,
                 font_size: int = 18) -> str:
    """Обвести места `(x, y, w, h)` рамкой и указать на них стрелкой с подписью; вернуть путь.

    Плашка с подписью ставится в самое пустое место рядом с рамкой (см. docstring
    модуля), кончик стрелки — на краю рамки.

    Args:
        marks: [{'box': (x, y, w, h), 'label': 'текст'}] — пиксели кадра.
        out: куда сохранить; пусто — поверх `path`.
    """
    img = Image.open(path).convert('RGB')
    gray = img.convert('L')
    draw = ImageDraw.Draw(img)
    font = image_font(font_size)
    frames = [_frame([int(v) for v in m['box']]) for m in marks]
    taken = list(frames)
    for mark, frame in zip(marks, frames):
        draw.rounded_rectangle(frame, radius=8, outline=color, width=ARROW_WIDTH - 1)
        label = mark.get('label', '')
        if not label:
            continue
        size = (int(draw.textlength(label, font=font)) + 20, font_size + 12)
        plate = _place(gray, frame, size, taken)
        taken.append(plate)
        tail, tip = _ends(plate, frame)
        _arrow(draw, tail, tip, color)
        draw.rounded_rectangle(plate, radius=6, fill=color)
        draw.text((plate[0] + 10, plate[1] + 4), label, fill='white', font=font)
    target = out or path
    img.save(target)
    return target


def _frame(box: list) -> tuple:
    x, y, w, h = box
    return (x - ARROW_PAD, y - ARROW_PAD, x + w + ARROW_PAD, y + h + ARROW_PAD)


def _place(gray, frame: tuple, size: tuple, taken: list) -> tuple:
    """Лучшее место плашки `size` рядом с рамкой: пусто под ней и на линии, ни на что не налезает."""
    pw, ph = size
    cx, cy = (frame[0] + frame[2]) / 2, (frame[1] + frame[3]) / 2
    best, best_score = None, None
    for dist in ARROW_DISTANCES:
        for dx, dy in ARROW_DIRECTIONS:
            px = (frame[2] + dist if dx > 0 else frame[0] - dist - pw if dx < 0 else cx - pw / 2)
            py = (frame[3] + dist if dy > 0 else frame[1] - dist - ph if dy < 0 else cy - ph / 2)
            plate = (int(px), int(py), int(px) + pw, int(py) + ph)
            if plate[0] < 0 or plate[1] < 0 or plate[2] > gray.width or plate[3] > gray.height:
                continue
            if any(_overlap(plate, t, 4) for t in taken):
                continue
            tail, tip = _ends(plate, frame)
            score = _busy(gray, plate) * 2 + _busy_line(gray, tail, tip) + dist * 0.05
            if best_score is None or score < best_score:
                best, best_score = plate, score
    if best is None:  # места нет нигде — под рамкой, как есть
        best = (int(frame[0]), int(frame[3]) + 10, int(frame[0]) + pw, int(frame[3]) + 10 + ph)
    return best


def _ends(plate: tuple, frame: tuple) -> tuple:
    """Хвост — ближайшая к рамке точка плашки, кончик — ближайшая к плашке точка рамки."""
    pcx, pcy = (plate[0] + plate[2]) / 2, (plate[1] + plate[3]) / 2
    tip = (min(max(pcx, frame[0]), frame[2]), min(max(pcy, frame[1]), frame[3]))
    tail = (min(max(tip[0], plate[0]), plate[2]), min(max(tip[1], plate[1]), plate[3]))
    return tail, tip


def _busy(gray, box: tuple) -> float:
    return ImageStat.Stat(gray.crop(box)).stddev[0]


def _busy_line(gray, a: tuple, b: tuple, steps: int = 24) -> float:
    vals = []
    for i in range(steps + 1):
        x = int(a[0] + (b[0] - a[0]) * i / steps)
        y = int(a[1] + (b[1] - a[1]) * i / steps)
        if 0 <= x < gray.width and 0 <= y < gray.height:
            vals.append(gray.getpixel((x, y)))
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _overlap(a: tuple, b: tuple, gap: int = 0) -> bool:
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _arrow(draw, tail, tip, color):
    draw.line([tail, tip], fill=color, width=ARROW_WIDTH)
    ang = math.atan2(tip[1] - tail[1], tip[0] - tail[0])
    size = ARROW_WIDTH * 4
    left = (tip[0] - size * math.cos(ang - 0.45), tip[1] - size * math.sin(ang - 0.45))
    right = (tip[0] - size * math.cos(ang + 0.45), tip[1] - size * math.sin(ang + 0.45))
    draw.polygon([tip, left, right], fill=color)


def _cli(argv=None):
    ap = argparse.ArgumentParser(description='Стрелки с подписями на скриншоте.')
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--mark', action='append', required=True, metavar='X,Y,W,H=ПОДПИСЬ')
    ap.add_argument('--color', default=ARROW_COLOR)
    a = ap.parse_args(argv)
    marks = []
    for item in a.mark:
        box, _, label = item.partition('=')
        marks.append({'box': tuple(int(v) for v in box.split(',')), 'label': label})
    print(image_arrows(a.src, marks, a.out, a.color))


if __name__ == '__main__':
    _cli()

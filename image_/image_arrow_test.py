"""Тесты `image_arrow` — плашки меток на длинном теле: справа у своей строки, без пересечений."""
from PIL import Image, ImageDraw

from image_.image_arrow import ARROW_COLOR, _frame, _overlap, _place, image_arrows

BODY_W, BODY_H, ROW = 900, 40 + 110 * 20, 20


def _long_body(tmp_path, full_width=False):
    """Кадр длинного JSON-тела: строки текста слева (справа пусто) тёмный фон."""
    path = str(tmp_path / 'body.png')
    img = Image.new('RGB', (BODY_W, BODY_H), (15, 23, 42))
    draw = ImageDraw.Draw(img)
    for i in range(BODY_H // ROW):
        x2 = BODY_W - 40 if full_width else 300
        draw.rectangle((40, 40 + i * ROW, x2, 46 + i * ROW), fill=(226, 232, 240))
    img.save(path)
    return path


def test_nearest_free_slot_right_of_the_line(tmp_path):
    gray = Image.open(_long_body(tmp_path)).convert('L')
    frame = _frame([40, 140, 220, 10])
    plate = _place(gray, frame, (80, 30), [frame])
    assert plate[0] >= frame[2]                       # справа от строки
    assert abs((plate[1] + plate[3]) / 2 - (frame[1] + frame[3]) / 2) <= 6  # на её высоте


def test_adjacent_lines_spread_right_not_down(tmp_path):
    """Тот самый кейс: две метки near the top длинного тела — у своих строк, стрелки горизонтальны."""
    gray = Image.open(_long_body(tmp_path)).convert('L')
    f1, f2 = _frame([40, 140, 220, 10]), _frame([40, 160, 220, 10])
    taken = [f1, f2]
    p1 = _place(gray, f1, (80, 30), taken)
    assert not any(_overlap(p1, other, 4) for other in taken)
    taken.append(p1)
    p2 = _place(gray, f2, (80, 30), taken)
    assert not any(_overlap(p2, other, 4) for other in taken)
    for plate, frame in ((p1, f1), (p2, f2)):
        assert plate[0] >= frame[2] and plate[3] < frame[3] + 60      # у своей строки, не внизу кадра
    assert p1[2] <= p2[0] + 4 or p2[2] <= p1[0] + 4                   # плашки рядом, без наездов


def test_falls_back_to_scoring_when_right_is_full(tmp_path):
    gray = Image.open(_long_body(tmp_path, full_width=True)).convert('L')
    frame = _frame([40, 140, 220, 10])
    plate = _place(gray, frame, (80, 30), [frame])
    assert not _overlap(plate, frame, 4)
    assert 0 <= plate[0] and plate[2] <= gray.width and 0 <= plate[1] and plate[3] <= gray.height


def test_arrows_land_near_their_lines_end_to_end(tmp_path):
    path = _long_body(tmp_path)
    out = image_arrows(path, [{'box': (40, 140, 220, 10), 'label': 'amount'},
                              {'box': (40, 160, 220, 10), 'label': 'currency'}],
                       str(tmp_path / 'marked.png'))
    img = Image.open(out)
    color = (225, 29, 72)
    rows = {y for y in (145, 165) for x in range(300, BODY_W) if img.getpixel((x, y)) == color}
    assert rows == {145, 165}      # плашки у своих строк, а не внизу кадра

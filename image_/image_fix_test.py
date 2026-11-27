"""Тесты `image_fix_blur` — офлайн, на PIL-картинках без внешних данных."""
from PIL import Image

from image_.image_fix import image_fix_blur


def _canvas(tmp_path, mode='RGB'):
    src = tmp_path / 'shot.png'
    img = Image.new(mode, (200, 100), (255, 255, 255, 255) if mode == 'RGBA' else 'white')
    for x in range(50, 70):        # чёрный квадрат — «значение email»
        for y in range(40, 60):
            img.putpixel((x, y), (0, 0, 0, 255) if mode == 'RGBA' else (0, 0, 0))
    img.save(src)
    return str(src)


def test_image_fix_blur_размывает_край_и_не_трогает_чистое(tmp_path):
    src = _canvas(tmp_path)
    out = str(tmp_path / 'blurred.png')
    got = image_fix_blur(src, out, [(40, 30, 80, 70)], radius=6)
    assert got['boxes'] == 1 and got['radius'] == 6
    px = Image.open(out).load()
    edge = px[70, 40]              # пиксель сразу за чёрным краем — обязан посереть
    assert 15 < edge[0] < 245, edge
    assert px[60, 50][0] < 120       # середина квадрата — ещё тёмная, но уже не 0
    assert px[10, 10] == (255, 255, 255)  # вне окна — нетронуто


def test_image_fix_blur_держит_альфу(tmp_path):
    src = _canvas(tmp_path, mode='RGBA')
    out = str(tmp_path / 'blurred.png')
    image_fix_blur(src, out, [(40, 30, 80, 70)], radius=6)
    got = Image.open(out)
    assert got.mode == 'RGBA'
    assert got.getpixel((10, 10))[3] == 255 and got.getpixel((70, 40))[3] == 255


def test_image_fix_blur_окно_за_краем_не_считается(tmp_path):
    src = _canvas(tmp_path)
    got = image_fix_blur(src, str(tmp_path / 'b.png'),
                         [(500, 0, 700, 10), (0, 0, 5, 5)], radius=4)
    assert got['boxes'] == 1     # первое окно целиком за кадром — его нет

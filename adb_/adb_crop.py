"""
Вырезка с экрана в натуральную величину — читать мелкое и не пересчитывать координаты.

Снимок целиком читатель уменьшает под свой предел: экран 1080×2400 приходит к нему
922×2048. Отсюда две беды сразу — мелкий текст перестаёт читаться, а всё, что на
таком снимке замечено, живёт в чужом масштабе: координату приходится домножать на
глазок, и нажатие уходит мимо.

Вырезка снимает обе. Область берётся в исходных пикселях и не уменьшается, поэтому
надпись видно; а `adb_crop_point` переводит замеченное на вырезке обратно в
координаты экрана — множитель считать не нужно.

Когда вырезка не нужна: элемент есть в дампе — координата у него уже точная, и
`adb_ui`/`adb_step` дешевле и вернее. Вырезка — про то, чего в дереве нет вовсе:
картинки, коды, номера на карточке, экраны на Canvas и WebView.
"""
import argparse
import io
import os
import tempfile

from PIL import Image

from adb_.adb_ import adb_capture
from adb_.adb_ui import adb_ui_find

# Запас вокруг элемента: границы из дампа режут ровно по надписи, и вырезка по ним
# выходит без полей — читать её труднее, чем ту же надпись с отступом.
ADB_CROP_PAD = 24

# Именованные области: доли экрана `(слева, сверху, справа, снизу)`. Половина
# экрана, а центр — середина по обеим сторонам.
ADB_CROP_PARTS = {
    'top': (0.0, 0.0, 1.0, 0.5),
    'bottom': (0.0, 0.5, 1.0, 1.0),
    'left': (0.0, 0.0, 0.5, 1.0),
    'right': (0.5, 0.0, 1.0, 1.0),
    'center': (0.25, 0.25, 0.75, 0.75),
}


def adb_crop_box(box, serial: str = '', path: str = '', source: str = '') -> dict:
    """
    Вырезать прямоугольник экрана в натуральную величину.

    Args:
        box: `(слева, сверху, справа, снизу)` в пикселях экрана; вылезающее за
            край подрезается.
        serial: устройство; пусто — единственное подключённое.
        path: куда сохранить вырезку; пусто — во временный файл.
        source: готовый снимок файлом; пусто — снять свой.

    Returns:
        Словарь `{'path', 'box', 'size'}`: `box` — область после подрезки,
        `size` — размер вырезки. Точку с вырезки переводит `adb_crop_point`.

    Raises:
        ValueError: область пустая или целиком вне экрана.
    """
    return _crop_save(_image_open(serial, source), box, path)


def adb_crop_on(query: str, serial: str = '', pad: int = ADB_CROP_PAD,
                path: str = '', exact: bool = False) -> dict:
    """
    Вырезать элемент по надписи — с запасом вокруг.

    Границы берутся из дампа, а не с глаза: `adb_ui` знает их точно.

    Args:
        query: надпись элемента или её часть.
        serial: устройство; пусто — единственное подключённое.
        pad: запас вокруг элемента, пиксели.
        path: куда сохранить вырезку; пусто — во временный файл.
        exact: только полное совпадение надписи.

    Returns:
        Словарь вырезки (см. `adb_crop_box`).

    Raises:
        RuntimeError: надписи на экране нет либо у элемента нет своих границ
            (ярлык вкладки живёт координатами предка) — тогда режут по области.
    """
    node = adb_ui_find(query, serial=serial, exact=exact)
    if not node:
        raise RuntimeError(f'надписи «{query}» на экране нет — карта в adb_ui_text()')
    bounds = node['bounds']
    if len(bounds) != 4 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise RuntimeError(f'у элемента «{query}» нет своих границ — режь по области '
                           f'или по точке нажатия {node["tap"]}')
    return adb_crop_box((bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad),
                        serial=serial, path=path)


def adb_crop_part(name: str = 'top', serial: str = '', path: str = '',
                  source: str = '') -> dict:
    """
    Вырезать именованную область экрана — не считая пикселей.

    Args:
        name: `top`, `bottom`, `left`, `right` или `center`.
        serial: устройство; пусто — единственное подключённое.
        path: куда сохранить вырезку; пусто — во временный файл.
        source: готовый снимок файлом; пусто — снять свой.

    Returns:
        Словарь вырезки (см. `adb_crop_box`).

    Raises:
        ValueError: область названа не из списка.
    """
    if name not in ADB_CROP_PARTS:
        raise ValueError(f'область «{name}»: ожидается {", ".join(ADB_CROP_PARTS)}')
    image = _image_open(serial, source)
    width, height = image.size
    left, top, right, bottom = ADB_CROP_PARTS[name]
    return _crop_save(image, (left * width, top * height, right * width, bottom * height), path)


def adb_crop_point(crop: dict, x: int, y: int, seen: tuple = ()) -> tuple:
    """
    Точка с вырезки — в координаты экрана.

    Args:
        crop: словарь вырезки.
        x: координата по горизонтали на вырезке.
        y: координата по вертикали на вырезке.
        seen: размер вырезки таким, каким её увидел читатель, — если он всё же
            уменьшил её под свой предел. Пусто — вырезка читалась как есть.

    Returns:
        `(x, y)` в пикселях экрана — этой парой и нажимают.
    """
    width, height = crop['size']
    if len(seen) == 2 and seen[0] and seen[1] and tuple(seen) != (width, height):
        x, y = x * width / seen[0], y * height / seen[1]
    return int(crop['box'][0] + x), int(crop['box'][1] + y)


def adb_crop_describe(crop: dict, prompt: str = '', model_name: str = '') -> str:
    """
    Показать вырезку vision-модели.

    Мелкое на вырезке читается там, где на целом экране не читалось: модель тоже
    уменьшает картинку под свой предел, и мелкий текст съедается им, а не моделью.

    Args:
        crop: словарь вырезки.
        prompt: вопрос; пусто — подробное описание с транскрипцией.
        model_name: vision-модель с префиксом сервиса; пусто — по умолчанию.

    Returns:
        Текст ответа модели.
    """
    from ai.ai_vision import ai_vision_describe_wait
    with open(crop['path'], 'rb') as fh:
        return ai_vision_describe_wait(fh.read(), prompt=prompt, model_name=model_name)


def _image_open(serial: str, source: str) -> Image.Image:
    """Снимок для резки: готовый файл или свежий с устройства, в натуральном разрешении."""
    if source:
        return Image.open(source)
    return Image.open(io.BytesIO(adb_capture(serial=serial, max_side=0)))


def _crop_save(image: Image.Image, box, path: str) -> dict:
    """Вырезать и записать; качество высокое — вырезку затем и читают, что на ней мелкое."""
    fit = _box_fit(box, image.size)
    out = _crop_path(path)
    image.crop(fit).save(out, quality=95)
    return {'path': out, 'box': fit, 'size': (fit[2] - fit[0], fit[3] - fit[1])}


def _box_fit(box, size: tuple) -> tuple:
    """Область в границах экрана, целыми числами; пустая после подрезки — ошибка, а не пустой файл."""
    width, height = size
    left, top = max(0, int(box[0])), max(0, int(box[1]))
    right, bottom = min(width, int(box[2])), min(height, int(box[3]))
    if right <= left or bottom <= top:
        raise ValueError(f'область {tuple(int(value) for value in box)} пуста на экране {width}x{height}')
    return left, top, right, bottom


def _crop_path(path: str, name: str = 'adb_crop.jpg') -> str:
    """Куда писать: названный путь или временный файл рядом с прочими."""
    return path or os.path.join(tempfile.gettempdir(), name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Вырезка с экрана в натуральную величину.',
        epilog="on 'Меню' | box 100 200 500 700 | part top")
    parser.add_argument('command', choices=['on', 'box', 'part'])
    parser.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    parser.add_argument('--out', default='', help='куда сохранить вырезку')
    parser.add_argument('--pad', type=int, default=ADB_CROP_PAD, help='запас вокруг элемента (on)')
    parser.add_argument('--source', default='', help='резать готовый снимок, а не снимать свой')
    parser.add_argument('args', nargs='*', help='надпись, координаты области или её имя')
    ns = parser.parse_args()

    try:
        if ns.command == 'on':
            cut = adb_crop_on(' '.join(ns.args), serial=ns.serial, pad=ns.pad, path=ns.out)
        elif ns.command == 'box':
            cut = adb_crop_box([int(value) for value in ns.args[:4]], serial=ns.serial,
                               path=ns.out, source=ns.source)
        else:
            cut = adb_crop_part(ns.args[0] if ns.args else 'top', serial=ns.serial,
                                path=ns.out, source=ns.source)
        print('{}\n{}x{} из [{},{}] — точку (x, y) с вырезки читать как ({} + x, {} + y)'.format(
            cut['path'], cut['size'][0], cut['size'][1],
            cut['box'][0], cut['box'][1], cut['box'][0], cut['box'][1]))
    except (IndexError, ValueError) as err:
        raise SystemExit(f'ошибка в аргументах: {err}')
    except (RuntimeError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')

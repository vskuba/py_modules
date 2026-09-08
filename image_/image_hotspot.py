"""
Хотспоты глазами: прямоугольники поверх снимка и готовая разметка `<a>`.

Арифметика долей уже есть в `image_.image_` (команда `frac`, `image_frac`):
пиксели ↔ доли и vw с вычетом кроя. Чего там нет и зачем этот модуль:

- наложение: числа верны, но глаз верит картинке — рамки с подписями прямо на
  снимке отвечают «на месте ли окно» до того, как hotspot уедет в вёрстку и
  вранье всплывёт на телефоне;
- разметка: из прямоугольника — сразу `<a href=... style="position:absolute;
  left:..%">` с процентами от кадра за вычетом кроя (статус-бара), подписью
  `aria-label` и классом; копипаста «посчитать − вписать − опечататься»
  исчезает.

Координаты входных прямоугольников — пиксели целого кадра (как прилетают со
снимка экрана), крой — PIL-box `(x0, y0, x1, y1)`, та же запись, что у
`image_frac` и `.crop(...)`.
"""
import argparse
import json
import os

from PIL import Image, ImageDraw


def image_hotspot_overlay(path: str, rects: list, out: str = '',
                          crop: tuple = None, color: str = 'red',
                          width: int = 6, labels: list = None) -> dict:
    """
    Нарисовать прямоугольники хотспотов поверх снимка — проверка «на месте ли».

    Args:
        path: снимок (физические пиксели целого кадра).
        rects: прямоугольники `(x, y, w, h)` в пикселях целого кадра.
        out: куда положить PNG; пусто — рядом с оригиналом `<stem>-hotspots.png`.
        crop: PIL-box вырезаемого окна целого кадра; координаты рисуются как
            есть (они от целого кадра), крой нужен только чтобы сообщить,
            что видит страница.
        color: цвет рамки (имя или hex PIL).
        width: толщина рамки, пиксели.
        labels: подписи по порядку (у каждого rect'а — свой).

    Returns:
        {'file', 'out', 'drawn', 'crop'} — координаты в 'out' не пересчитаны:
        рамка рисуется по пикселям входа.
    """
    img = Image.open(path).convert('RGB')
    draw = ImageDraw.Draw(img)
    labels = labels or []
    boxes = []
    for i, rect in enumerate(rects):
        x, y, w, h = (int(v) for v in rect)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=width)
        if i < len(labels) and labels[i]:
            draw.text((x + width + 2, max(0, y - 2)), str(labels[i]), fill=color)
        boxes.append({'x': x, 'y': y, 'w': w, 'h': h, 'label': labels[i] if i < len(labels) else ''})
    out = out or (os.path.splitext(path)[0] + '-hotspots.png')
    img.save(out)
    return {'file': path, 'out': out, 'drawn': len(boxes),
            'crop': tuple(crop) if crop else None}


def image_hotspot_markup(rects: list, hrefs: list = None, labels: list = None,
                         crop: tuple = None, path: str = '', cls: str = '') -> dict:
    """
    Сгенерировать разметку `<a>` по хотспотам: проценты кадра за вычетом кроя.

    Args:
        rects: прямоугольники `(x, y, w, h)` в пикселях целого кадра.
        hrefs: ссылки по порядку; пусто — `#`.
        labels: `aria-label` по порядку.
        crop: PIL-box вырезаемого окна (то, что видит страница); проценты
            считаются от него. Пусто — от целого кадра (`path` обязателен).
        path: снимок целого кадра — нужен, только когда crop не задан (взять
            размеры кадра).
        cls: класс каждой ссылки (например `zone`).

    Returns:
        {'links': [str], 'frame': {'w','h'} отсчёта, 'crop'} — строки готовы
        в `<div class="zones">` с `position:absolute` родителем.
    """
    if crop:
        cx0, cy0, cx1, cy1 = (int(v) for v in crop)
        cw, ch = cx1 - cx0, cy1 - cy0
    else:
        if not path:
            raise ValueError('без crop нужен path — размеры кадра взять неоткуда')
        with Image.open(path) as img:
            cw, ch = img.size
        cx0 = cy0 = 0
    links = []
    for i, rect in enumerate(rects):
        x, y, w, h = (int(v) for v in rect)
        style = 'position:absolute;' + ';'.join(
            f'{k}:{round(v * 100, 4)}%' for k, v in
            (('left', (x - cx0) / cw), ('top', (y - cy0) / ch),
             ('width', w / cw), ('height', h / ch))) + ';'
        attrs = [f'href="{(hrefs or [])[i] if hrefs and i < len(hrefs) else "#"}"']
        if cls:
            attrs.append(f'class="{cls}"')
        if labels and i < len(labels) and labels[i]:
            attrs.append(f'aria-label="{labels[i]}"')
        links.append('<a ' + ' '.join(attrs) + f' style="{style}"></a>')
    return {'links': links, 'frame': {'w': cw, 'h': ch},
            'crop': tuple(crop) if crop else None}


def _rects(specs: list) -> tuple:
    """Спеки `x,y,w,h[:подпись]` → (rects, labels); та же запись, что у `--rect` в `image_`."""
    rects, labels = [], []
    for spec in specs:
        rect, _, label = spec.partition(':')
        parts = [int(v) for v in rect.split(',')]
        if len(parts) != 4:
            raise ValueError(f'прямоугольник {spec!r}: ждём x,y,w,h[:подпись]')
        rects.append(tuple(parts))
        labels.append(label)
    return rects, labels


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Хотспоты: рамки поверх снимка и разметка <a> процентами.')
    ap.add_argument('command', choices=['overlay', 'markup'])
    ap.add_argument('shot', help='снимок целого кадра')
    ap.add_argument('--rect', action='append', default=[],
                    help='x,y,w,h[:подпись], повторно — сколько хотспотов нужно')
    ap.add_argument('--crop', default='', help='PIL-box x0,y0,x1,y1 окна страницы')
    ap.add_argument('--out', default='', help='PNG наложений (overlay)')
    ap.add_argument('--color', default='red', help='цвет рамки (overlay)')
    ap.add_argument('--href', action='append', default=[], help='ссылки по порядку (markup)')
    ap.add_argument('--class', dest='cls', default='', help='класс ссылок (markup)')
    ns = ap.parse_args()
    try:
        rects, labels = _rects(ns.rect)
        crop = tuple(int(v) for v in ns.crop.split(',')) if ns.crop else None
        if ns.command == 'overlay':
            res = image_hotspot_overlay(ns.shot, rects, out=ns.out, crop=crop,
                                        color=ns.color, labels=labels)
            print(f"{res['out']} ({res['drawn']} рамок)")
        else:
            res = image_hotspot_markup(rects, hrefs=ns.href, labels=labels,
                                       crop=crop, path=ns.shot, cls=ns.cls)
            print('\n'.join(res['links']))
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

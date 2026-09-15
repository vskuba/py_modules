"""
Контактные листы: глазами выбрать, что брать за основу.

Пять раз за вечер лист рисовался heredoc'ом: сетка, номер, время, имя —
а потом ещё «одно и то же лицо из двадцати кадров рядом». Модуль делает оба
листа одной строкой: сетка файлов с подписями (№, время mtime, имя) и ряд
одинаковых кропов по боксу/точкам — выбор глазами становится командой,
а не сочинением PIL каждый раз.
"""
import argparse
from datetime import datetime
from pathlib import Path

IMAGE_SHEET_THUMB = 300   # сторона превью, px
IMAGE_SHEET_COLS = 7      # колонок в сетке
IMAGE_SHEET_BAR = 30      # высота подписи под кадром, px


def _grid(files, out, cols, thumb, label):
    from PIL import Image, ImageDraw
    import os
    files = [Path(f) for f in files]
    files.sort(key=os.path.getmtime)
    rows = -(-len(files) // cols)
    sheet = Image.new('RGB', (cols * thumb, rows * (thumb + IMAGE_SHEET_BAR)),
                     'white')
    d = ImageDraw.Draw(sheet)
    for i, f in enumerate(files):
        im = Image.open(f).convert('RGB')
        im.thumbnail((thumb, thumb))
        x, y = (i % cols) * thumb, (i // cols) * (thumb + IMAGE_SHEET_BAR)
        sheet.paste(im, (x, y + IMAGE_SHEET_BAR))
        d.rectangle([x, y, x + thumb, y + IMAGE_SHEET_BAR], fill=(240, 240, 240))
        t = datetime.fromtimestamp(os.path.getmtime(f)).strftime('%d.%d %H:%M')
        d.text((x + 6, y + 2), f'№{i:02d}  {t if label == "mtime" else f.name}',
               fill='red')
        d.text((x + 6, y + 16), f.name[:40], fill='black')
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return {'out': str(out), 'n': len(files)}


def image_sheet(files, out, cols=IMAGE_SHEET_COLS, thumb=IMAGE_SHEET_THUMB,
                label='mtime'):
    """Сетка кадров по времени mtime; вернуть {'out','n'}.

    Порядок — по времени, чтобы ветка ночных прогонов читалась слева направо.
    `label`: 'mtime' | 'name' | '' — что дублировать в подписи под номером.
    """
    return _grid(files, out, cols, thumb, label)


def image_sheet_crops(files, out, box=None, mode='raw', cols=IMAGE_SHEET_COLS,
                      thumb=IMAGE_SHEET_THUMB):
    """Одна и та же область с каждого кадра в ряд; вернуть {'out','n'}.

    Кроит по `box` (абсолютные координаты кадра) или по точке лица
    (`mode`='face'/'face_hair'/'head_neck` — ai_landmark расширит bbox).
    Досадливо: кадр меньше кропа не лопается, а режется по фактическим пикселям.
    """
    import cv2
    import numpy as np
    from ai.ai_landmark import ai_landmark, ai_landmark_ellipse
    crops = []
    for f in map(Path, files):
        img = cv2.imread(str(f))
        if img is None:
            continue
        if box:
            x0, y0, x1, y1 = (int(v) for v in box)
        elif mode != 'raw':
            lm = ai_landmark(f)
            x0, y0, x1, y1 = ai_landmark_ellipse(lm['bbox'], lm['kps'], mode=mode)
        else:
            x0, y0, x1, y1 = 0, 0, img.shape[1], img.shape[0]
        crops.append((cv2.cvtColor(img[max(0, y0):min(img.shape[0], y1),
                                      max(0, x0):min(img.shape[1], x1)],
                                   cv2.COLOR_BGR2RGB), f))
    from PIL import Image
    tmp = [Path(out).with_name(f'{f.stem}-кроп.png') for _, f in crops]
    for (c, _), t in zip(crops, tmp):
        Image.fromarray(c).save(t)
    got = _grid(tmp, out, cols, thumb, 'name')
    for t in tmp:
        t.unlink()
    return got


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='image_sheet',
                                 description='контактные листы и нумерованные кропы')
    ap.add_argument('files', nargs='+', help='файлы или globs')
    ap.add_argument('--out', required=True)
    ap.add_argument('--cols', type=int, default=IMAGE_SHEET_COLS)
    ap.add_argument('--thumb', type=int, default=IMAGE_SHEET_THUMB)
    ap.add_argument('--label', default='mtime')
    ap.add_argument('--box', default='', help='кроп x0,y0,x1,y1; пусто — весь кадр')
    ap.add_argument('--mode', default='raw')
    ns = ap.parse_args()
    import glob as g
    files = [p for pat in ns.files for p in g.glob(pat)]
    if ns.box:
        b = tuple(int(v) for v in ns.box.split(','))
        print(image_sheet_crops(files, ns.out, box=b, mode=ns.mode,
                                cols=ns.cols, thumb=ns.thumb))
    else:
        print(image_sheet(files, ns.out, cols=ns.cols, thumb=ns.thumb,
                          label=ns.label))

"""
Замер зон кадра: что и насколько изменилось, где край заплатки — цифрами.

Раунды спорили вслепую «лицо жёлтое / квадрат видно», пока каждый раз не
придумывался heredoc-замер. Теперь один вызов: медианы зон композита против
оригинала, Δ по ним, максимум вне области вшивки (должен быть 0 — вне маски
кадр байт в байт) и скачок на самой границе (перо сглаживает, большой скачок
значит маска резала лицо).

Зоны берутся из геометрии точек (лоб/щёки/подбородок/тело), а не на глаз:
те же доли, что режет маска ai_face_paste.
"""
import argparse
import json


def _core(px):
    import numpy as np
    y = px @ np.array([0.114, 0.587, 0.299], 'float32')
    lo, hi = np.percentile(y, [25, 75])
    return px[(y >= lo) & (y <= hi)] if len(px) >= 8 else px


def image_probe_zones(box, kps=None, body=0.45):
    """{'имя зоны': (x0,y0,x1,y1)} по боксу и точкам — те же доли, что маска."""
    x0, y0, x1, y1 = (int(v) for v in box)
    if not kps:
        return {'верх': (x0, y0, x1, y0 + int((y1 - y0) * 0.3)),
                'ядро': (x0 + int((x1 - x0) * .25), y0 + int((y1 - y0) * .3),
                         x1 - int((x1 - x0) * .25), y0 + int((y1 - y0) * .85)),
                'низ': (x0, y1 - int((y1 - y0) * .15), x1, y1)}
    ex, my = (kps[0][1] + kps[1][1]) / 2, (kps[3][1] + kps[4][1]) / 2
    fh = kps[4][1] - kps[0][1]
    return {'над овалом': (x0, y0, x1, int(ex - fh * 0.35)),
            'лоб': (x0 + 60, int(ex - fh * 0.35), x1 - 60, int(my - fh * 0.1)),
            'щёки': (x0 + 60, int(my), x1 - 60, y1 - 40),
            'под овалом': (x0, int(my + fh * 0.6), x1, y1),
            'тело': (x0, y1, x1, int(y1 + (y1 - y0) * body))}


def image_probe_diff(composite, source, box=None, kps=None):
    """Зоны композита против оригинала; {'zones','outside','seam'}.

    `zones[имя]` = {'now','was','delta'} — медианы RGB (RGB, не BGR) зоны
    композита и оригинала и их разница; `outside` — max |Δ| вне бокса (0 =
    кадр вне вшивки не тронут); `seam` — max |Δ| на полосе 2 px у границы бокса
    изнутри (большой — маска резала лицо, малый — перо работает).
    """
    import cv2
    import numpy as np
    comp = cv2.imdecode(np.frombuffer(open(composite, 'rb').read(), np.uint8),
                        cv2.IMREAD_COLOR)
    src = cv2.imdecode(np.frombuffer(open(source, 'rb').read(), np.uint8),
                       cv2.IMREAD_COLOR)
    if comp is None or src is None:
        raise ValueError('не прочитана пара кадров для probe')
    box = box or [0, 0, comp.shape[1], comp.shape[0]]
    zones = image_probe_zones(box, kps)
    out = {'zones': {}, 'outside': 0, 'seam': 0}
    bx0, by0, bx1, by1 = (int(v) for v in box)
    for name, (a, b, c, d) in zones.items():
        h, w = comp.shape[:2]
        a, b, c, d = max(0, a), max(0, b), min(w, c), min(h, d)
        if c <= a or d <= b:
            continue
        now = np.median(_core(comp[b:d, a:c].reshape(-1, 3).astype('float32')),
                        0)[::-1]
        was = np.median(_core(src[b:d, a:c].reshape(-1, 3).astype('float32')),
                        0)[::-1]
        out['zones'][name] = {'now': [round(float(v), 1) for v in now],
                              'was': [round(float(v), 1) for v in was],
                              'delta': [round(float(x), 1)
                                        for x in (now - was)]}
    h, w = comp.shape[:2]
    bx0, by0, bx1, by1 = max(0, bx0), max(0, by0), min(w, bx1), min(h, by1)
    d = abs(comp.astype('int16') - src.astype('int16'))
    out['outside'] = int(max([0] + ([int(d[:by0].max())] if by0 else []) +
                             ([int(d[by1:].max())] if by1 < h else []) +
                             ([int(d[:, :bx0].max())] if bx0 else []) +
                             ([int(d[:, bx1:].max())] if bx1 < w else [])))
    band = (comp[by0:by0 + 2, bx0:bx1].astype('int16') -
            src[by0:by0 + 2, bx0:bx1].astype('int16')) if by0 else []
    out['seam'] = int(np.abs(band).max()) if len(band) else 0
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='image_probe',
                                 description='замер зон и шва композита')
    ap.add_argument('composite')
    ap.add_argument('--source', default='')
    ap.add_argument('--box', default='')
    ap.add_argument('--kps', default='')
    ns = ap.parse_args()
    print(json.dumps(image_probe_diff(
        ns.composite, ns.source or ns.composite,
        [int(v) for v in ns.box.split(',')] if ns.box else None,
        json.loads(ns.kps) if ns.kps else None), ensure_ascii=False, indent=1))

"""
Доработка лица до деталей: крой → апскейл → резкость до кадра → обратно пером.

Пиксели генератора мягче пикселей фото: лицо из гена приходит мыльным, и глаз
это видит раньше, чем счётчик. Модуль поднимает детали там, где буст-прогон
фермы уже сделан или недоступен: кроп лица, Lanczos-апскейл (`scale`×), unsharp
до резкости того же кропа, поднятого с оригинала (не резче исходника), и
вшивка обратно эллипсом по точкам с широким пером. Если лицо приходит чужим
кропом с гена (`patch`) — подтяжка тона к коже тела кадра идёт перед вшивкой
(ai_look_fit), та же, что в диспетчере swap.

Если нужно само лицо ПЕРЕГЕНЕРИТЬ выкрученным сэмплингом — это
`comfy_gen swap --denoise/--detail`; здесь локальная доводка без фермы.
"""
import argparse
import json

AI_ENHANCE_SCALE = 2      # во сколько раз поднять кроп перед детальным проходом
AI_ENHANCE_FEATHER = 0.2  # широкое перо склейки (замер: узкое даёт квадрат)
AI_ENHANCE_STEPS = 3      # сколько unsharp-проходов максимум, пока не дотянули


def ai_enhance_face(path, out, patch='', box=None, kps=None,
                    scale=AI_ENHANCE_SCALE, feather=AI_ENHANCE_FEATHER):
    """Выкрутить детали лица; вернуть {'sharp_before','sharp_after','box','kps'}.

    `patch` — готовое лицо (кроп гена) вместо лица самого кадра; без него
    содержимое — кроп `path`. Кроп поднимается в `scale`× (Lanczos), доводится
    unsharp до резкости оригинального лица, тоже поднятого в `scale`× (цель —
    резкость кадра, не резче исходника), и вшивается эллипсом по kps с пером
    `feather`: вне маски кадр байт в байт. Чужая заплатка сначала подтягивает
    тон к коже тела кадра (ai_look_fit), как в диспетчере swap.
    """
    import cv2
    import numpy as np
    from pathlib import Path
    from ai.ai_face import ai_face_paste
    from ai.ai_look import ai_look_probe, ai_look_fit
    src = Path(path)
    if not box or kps is None:
        from ai.ai_landmark import ai_landmark
        lm = ai_landmark(src)
        box = list(box or lm['bbox'])
        kps = list(kps or lm['kps'])
    x0, y0, x1, y1 = (int(v) for v in box)
    src_img = cv2.imread(str(src))
    if src_img is None:
        raise ValueError(f'не прочитан кадр: {src}')
    face = src_img[y0:y1, x0:x1] if not patch else cv2.imread(str(patch))
    if face is None:
        raise ValueError(f'не прочитана заплатка: {patch}')
    big = lambda a: cv2.resize(a, ((x1 - x0) * scale, (y1 - y0) * scale),
                              interpolation=cv2.INTER_LANCZOS4)
    up = big(face)
    up_want = float(cv2.Laplacian(cv2.cvtColor(big(src_img[y0:y1, x0:x1]),
                                               cv2.COLOR_BGR2GRAY),
                                  cv2.CV_64F).var())
    gray = lambda a: cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    lap = lambda a: float(cv2.Laplacian(gray(a), cv2.CV_64F).var())
    before = round(lap(face), 1)
    now = round(lap(up), 1)
    step = 0
    while now < up_want * 0.95 and step < AI_ENHANCE_STEPS:
        up = np.clip(up.astype('float32') * 1.5 -
                     cv2.GaussianBlur(up, (3, 3), 0.8).astype('float32') * 0.5,
                     0, 255).astype('uint8')
        now = round(lap(up), 1)
        step += 1
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.face.png')
    cv2.imwrite(str(tmp), up)
    if patch:  # чужой ген: тон к коже тела кадра — как в диспетчере swap
        h_ = src_img.shape[0]
        strip = (x0, y1, x1, min(h_, y1 + int((y1 - y0) * 0.45)))
        ai_look_fit(tmp, ai_look_probe(src, strip), tmp)
    ai_face_paste(src, tmp, out, box, feather=feather, kps=kps, hf=0)
    tmp.unlink()
    return {'sharp_before': before, 'sharp_after': now, 'want': round(up_want, 1),
            'scale': scale, 'box': box, 'kps': kps}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='ai_enhance',
                                 description='доработка лица до деталей')
    ap.add_argument('path', help='кадр-основа')
    ap.add_argument('--out', required=True)
    ap.add_argument('--patch', default='', help='готовое лицо (кроп гена)')
    ap.add_argument('--box', default='')
    ap.add_argument('--kps', default='')
    ap.add_argument('--scale', type=int, default=AI_ENHANCE_SCALE)
    ap.add_argument('--feather', type=float, default=AI_ENHANCE_FEATHER)
    ns = ap.parse_args()
    print(json.dumps(ai_enhance_face(
        ns.path, ns.out, patch=ns.patch,
        box=[int(v) for v in ns.box.split(',')] if ns.box else None,
        kps=json.loads(ns.kps) if ns.kps else None,
        scale=ns.scale, feather=ns.feather), ensure_ascii=False, indent=1))

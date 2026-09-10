"""
Построчный оттенок кадра: профиль по каналам и вердикт «полоса».

Отвечает на вопрос, который не задаёт ни один замер окна: «тон плывёт по кадру
плавно или ступенькой». Глаз видит жёлтую полосу на светлом фоне раньше, чем
умеют моды и средние по окну, — те усредняют ровное поле с пятном и показывают
«всего чуть теплее». Здесь профиль считается построчно: для каждой строки —
медиана разности каналов (по умолчанию зелёный минус синий) по достаточно
ярким пикселям. Тёмные (текст, тени, обводки) в медиану не входят, поэтому
наличие букв в строке профиль не сдвигает.

Два среза: `image_tone_rows` — простой профиль `(y, значение)` для графика или
сравнения двух кадров поштучно, `image_tone_band` — вердикт одним числом:
максимальный скачок между соседними строками, его место и окрестности. Полосой
считается только ступенька (`band`); медленный градиент фона — норма обоев,
он остаётся «ровно».

Скриншот с телефона темнее того же кадра, запечённого в webp, — сравнение
ведут по разности каналов и её профилю, а не по абсолютному RGB
(грабли — `image_tooling.md`).
"""
import numpy as np
from PIL import Image

IMAGE_TONE_MIN_LUMA = 150   # порог «яркого» пикселя: ниже — текст и тени, не фон
IMAGE_TONE_STEP = 7         # прореживание выборки по x: медиане хватает семплов
IMAGE_TONE_MIN_JUMP = 6     # скачок между соседними строками, чтобы звать его полосой
IMAGE_TONE_CHANS = {'r': 0, 'g': 1, 'b': 2}


def image_tone_rows(path, rect=None, chan='g-b',
                    min_luma=IMAGE_TONE_MIN_LUMA, step=IMAGE_TONE_STEP) -> dict:
    """
    Профиль разности каналов по строкам: (y, медиана) для каждой строки окна.

    Args:
        path: картинка или снимок экрана.
        rect: (x0, y0, x1, y1) — окно замера; пусто — весь кадр.
        chan: разность каналов: 'g-b', 'b-g', 'r-b', 'r-g' …
        min_luma: пиксели темнее (минимум по каналам) в медиану не входят.
        step: прореживание выборки по x, пиксели.

    Returns:
        {'file', 'chan', 'rect', 'rows': [(y, медиана | None)], 'count'};
        медиана округлена до целого; None — в строке нет ни одного яркого пикселя.

    Raises:
        ValueError: неизвестный chan; rect не лежит в кадре.
    """
    a, x0, y0, x1, y1 = _window(path, rect)
    c1, c2 = _chan_pair(chan)
    band = a[y0:y1, x0:x1:step, :]
    bright = band.min(axis=2) >= min_luma
    diff = band[:, :, c1].astype(np.int32) - band[:, :, c2].astype(np.int32)
    rows = []
    for i in range(band.shape[0]):
        vals = diff[i][bright[i]]
        rows.append((y0 + i, int(round(float(np.median(vals)))) if vals.size else None))
    return {'file': path, 'chan': chan, 'rect': (x0, y0, x1, y1),
            'rows': rows, 'count': sum(v is not None for _, v in rows)}


def image_tone_band(path, rect=None, chan='g-b',
                    min_luma=IMAGE_TONE_MIN_LUMA, step=IMAGE_TONE_STEP,
                    min_jump=IMAGE_TONE_MIN_JUMP) -> dict:
    """
    Вердикт «полоса» по профилю: максимальный скачок между соседними строками.

    Полоса — ступенька в одном месте (фон до неё и после различаются тоном);
    медленный градиент, свойственный обоям и стеклу, полосой не считается.
    Строки без ярких пикселей из sequences выпадают, соседи определяются по
    ближайшим измеренным.

    Args:
        path, rect, chan, min_luma, step: как в `image_tone_rows`.
        min_jump: минимальная абсолютная величина скачка, `IMAGE_TONE_MIN_JUMP`.

    Returns:
        {'file', 'chan', 'rect', 'band': bool, 'band_y', 'jump', 'before',
         'after', 'max_jump'}; `band_y` — первая строка нового тона, `jump` —
         со знаком (после минус до), None — измеренных строк нет вовсе.
    """
    rows = [r for r in image_tone_rows(path, rect=rect, chan=chan,
                                       min_luma=min_luma, step=step)['rows']
            if r[1] is not None]
    if len(rows) < 2:
        return {'file': path, 'chan': chan, 'band': False, 'band_y': None,
                'jump': None, 'before': None, 'after': None, 'max_jump': None}
    best_i, best = 1, rows[1][1] - rows[0][1]
    for i in range(2, len(rows)):
        d = rows[i][1] - rows[i - 1][1]
        if abs(d) > abs(best):
            best_i, best = i, d
    return {'file': path, 'chan': chan, 'band': abs(best) >= min_jump,
            'band_y': rows[best_i][0], 'jump': best,
            'before': rows[best_i - 1][1], 'after': rows[best_i][1],
            'max_jump': max(abs(rows[i][1] - rows[i - 1][1])
                            for i in range(1, len(rows)))}


def _window(path, rect) -> tuple:
    """Кадр int32-массивом и проверенное окно; rect пусто — весь кадр."""
    img = np.asarray(Image.open(path).convert('RGB'), dtype=np.int32)
    h, w = img.shape[:2]
    if rect is None:
        return img, 0, 0, w, h
    x0, y0, x1, y1 = (int(v) for v in rect)
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError(f'rect {tuple(rect)} не лежит в кадре {w}x{h}')
    return img, x0, y0, x1, y1


def _chan_pair(chan) -> tuple:
    """Индексы каналов из записи 'g-b'."""
    parts = str(chan).split('-')
    if len(parts) != 2 or not all(p in IMAGE_TONE_CHANS for p in parts):
        raise ValueError(f"chan — разность вида 'g-b' из r, g, b; получено {chan!r}")
    return IMAGE_TONE_CHANS[parts[0]], IMAGE_TONE_CHANS[parts[1]]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description='Построчный оттенок кадра: профиль разности каналов и '
                    'вердикт «полоса или плавный градиент».')
    ap.add_argument('command', choices=['rows', 'band'])
    ap.add_argument('path', help='картинка или снимок экрана')
    ap.add_argument('--rect', default='', help='окно замера x0,y0,x1,y1; пусто — весь кадр')
    ap.add_argument('--chan', default='g-b', help="разность каналов: 'g-b', 'r-b' …")
    ap.add_argument('--min-luma', type=int, default=IMAGE_TONE_MIN_LUMA,
                    help='порог яркого пикселя, минимум по каналам')
    ap.add_argument('--step', type=int, default=IMAGE_TONE_STEP,
                    help='прореживание выборки по x')
    ap.add_argument('--min-jump', type=int, default=IMAGE_TONE_MIN_JUMP,
                    help='band: какой скачок считать полосой')
    ns = ap.parse_args()
    rect = tuple(int(v) for v in ns.rect.split(',')) if ns.rect else None

    try:
        if ns.command == 'rows':
            res = image_tone_rows(ns.path, rect=rect, chan=ns.chan,
                                  min_luma=ns.min_luma, step=ns.step)
            stride = max(1, len(res['rows']) // 80)
            print(f'{ns.path}: профиль {ns.chan}, окно {res["rect"]}, '
                  f'строк {res["count"]}')
            for y, v in res['rows'][::stride]:
                print(f'  y={y:>5} ' + ('—' if v is None else f'{v:+d}'))
        else:
            res = image_tone_band(ns.path, rect=rect, chan=ns.chan,
                                  min_luma=ns.min_luma, step=ns.step,
                                  min_jump=ns.min_jump)
            if res['band_y'] is None:
                print(f'{ns.path}: {ns.chan} — измерить нечего (нет ярких пикселей)')
            elif res['band']:
                print(f'{ns.path}: полоса y={res["band_y"]} — шаг {res["jump"]:+d}, '
                      f'до {res["before"]:+d}, после {res["after"]:+d}')
            else:
                print(f'{ns.path}: ровно — максимальный скачок {res["max_jump"]:+d}')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')


if __name__ == '__main__':
    main()

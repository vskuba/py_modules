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

Где страница залита градиентом, средние «карточка минус фон» по разным
строкам врут — для стекла и карточек есть второй фасон: `image_tone_row_delta`
меряет дельту в одной строке, `image_tone_edge` — профиль через границу
плитки (фон, кромка, панель), а `image_tone_glass_contract` /
`image_tone_glass_solve` / `image_tone_glass_verify` держат стеклянный
контракт `delta = a * (F - bg)`: заливка полупрозрачной пластины по мерке
оригинала и проверка клона живым снимком (пекут в `image_glass`).

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
IMAGE_TONE_GLASS_TOL = 1.0  # невязка проверки стеклянного контракта, на канал, единицы RGB


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


def image_tone_row_delta(path, y, bg, card, height=1) -> dict:
    """
    Карточка минус фон в той же строке: честный дельта на градиентной странице.

    Среднее «карточка минус зазор», взятое по разным строкам, врёт на 5–10
    единиц: фон страницы имеет вертикальный градиент, и зазор с карточкой
    приходятся на разные высоты. Здесь оба окна измеряются по одной строке
    `y` (полоса в `height` строк): `bg` — фон сбоку карточки, `card` — внутри
    неё. Глифы и текст внутри окна тянут среднее в темноту, поэтому окна
    выбирают там, где поле чистое.

    Args:
        path: картинка или снимок экрана.
        y: верх измерительной полосы, строка.
        bg: (x0, x1) — окно фона, за пределами карточки.
        card: (x0, x1) — окно карточки.
        height: высота полосы, строк.

    Returns:
        {'file', 'y', 'height', 'bg', 'card', 'bg_rgb', 'card_rgb', 'delta'};
        rgb — среднее по окну и каналу, округлённое до целого, `delta` —
        card_rgb минус bg_rgb, по трём каналам.

    Raises:
        ValueError: окна или полоса не лежат в кадре.
    """
    img, _, _, w, h = _window(path, None)
    bx0, bx1 = (int(v) for v in bg)
    cx0, cx1 = (int(v) for v in card)
    height = int(height)
    if not (0 <= bx0 < bx1 <= w and 0 <= cx0 < cx1 <= w):
        raise ValueError(f'окна bg {(bx0, bx1)} / card {(cx0, cx1)} не лежат в ширине {w}')
    if not (height >= 1 and 0 <= y < h and y + height <= h):
        raise ValueError(f'полоса y={y} height={height} не лежит в высоте {h}')
    bg_rgb = _rgb_mean(img, y, height, bx0, bx1)
    card_rgb = _rgb_mean(img, y, height, cx0, cx1)
    return {'file': path, 'y': int(y), 'height': height, 'bg': (bx0, bx1),
            'card': (cx0, cx1), 'bg_rgb': bg_rgb, 'card_rgb': card_rgb,
            'delta': tuple(c - b for c, b in zip(card_rgb, bg_rgb))}


def image_tone_edge(path, y, x, w=3, probe=None, height=1) -> dict:
    """
    Профиль через границу плитки: фон, кромка, панель и их дельта.

    Тёмную кромку (rim) стекла проверяют числом, а не глазом: среднее `w`
    столбцов перед границей (фон вне стекла), `w` столбцов начиная с `x`
    (сама кромка) и `w` столбцов сразу за ней (панель у стекла). Панель
    широка — её настоящий цвет берут из более глубокого окна `probe`
    (например, столбцы 300..800), и `delta` считает по нему: у самой
    границы colour ещё подкрашен кромкой. Метрика для калибровки альфы
    кромки в `image_glass`: альфа масштабирует «темноту» почти линейно
    (замер: 70/255 → −29, 40 → −21, 12 → −7 на кромке в 2 px). Меряют живым
    снимком экрана — в запечённом webp фона нет, те же числа выйдут другими.

    Args:
        path: снимок экрана.
        y: строка.
        x: граница — первая колонка кромки.
        w: ширина каждой из трёх зон, столбцов.
        probe: (x0, x1) — глубокое окно панели; пусто — дельта по зоне после границы.
        height: высота полосы, строк.

    Returns:
        {'file', 'y', 'x', 'w', 'before_rgb', 'edge_rgb', 'after_rgb',
         'probe_rgb', 'delta'}; `probe_rgb` — None, когда окно не задано,
         `delta` — edge_rgb минус probe_rgb (или minus after_rgb без probe).

    Raises:
        ValueError: зоны не лежат в кадре.
    """
    img, _, _, w_img, h = _window(path, None)
    w, y, x, height = int(w), int(y), int(x), int(height)
    if w < 1 or height < 1:
        raise ValueError(f'w и height — целые >= 1; получено w={w}, height={height}')
    if not (x - w >= 0 and x + 2 * w <= w_img):
        raise ValueError(f'зоны вокруг x={x} шириной {w} не лежат в ширине {w_img}')
    if not (0 <= y < h and y + height <= h):
        raise ValueError(f'полоса y={y} height={height} не лежит в высоте {h}')
    before = _rgb_mean(img, y, height, x - w, x)
    edge = _rgb_mean(img, y, height, x, x + w)
    after = _rgb_mean(img, y, height, x + w, x + 2 * w)
    probe_rgb = None
    if probe is not None:
        p0, p1 = (int(v) for v in probe)
        if not (0 <= p0 < p1 <= w_img):
            raise ValueError(f'окно probe {(p0, p1)} не лежит в ширине {w_img}')
        probe_rgb = _rgb_mean(img, y, height, p0, p1)
    ref = probe_rgb if probe_rgb is not None else after
    return {'file': path, 'y': y, 'x': x, 'w': w, 'before_rgb': before,
            'edge_rgb': edge, 'after_rgb': after, 'probe_rgb': probe_rgb,
            'delta': tuple(e - r for e, r in zip(edge, ref))}


def image_tone_glass_contract(bg, fill, alpha) -> dict:
    """
    Стеклянный контракт: delta = a * (F - bg).

    Полупрозрачная пластина цвета F с альфой `alpha` (0..255, как лежит в
    webp) поверх фона bg даёт на экране дельту `delta` — по этой формуле
    выбирают заливку клона по измеренной дельте оригинала
    (`image_tone_glass_solve`) и проверяют результат
    (`image_tone_glass_verify`). Формула работает и на светлой, и на тёмной
    фазе обоев: bg входит прямо, знак дельты задаёт разность цветов.

    Args:
        bg: (r, g, b) — фон: живой замер или цвет обоев.
        fill: (r, g, b) — цвет заливки пластины.
        alpha: альфа пластины, 0..255.

    Returns:
        {'bg', 'fill', 'alpha', 'share' — alpha/255, 'delta': (d0, d1, d2) —
         вещественные, до сотых}.

    Raises:
        ValueError: alpha вне (0, 255].
    """
    a = _glass_alpha(alpha)
    bg3, f3 = _rgb3(bg, 'bg'), _rgb3(fill, 'fill')
    return {'bg': bg3, 'fill': f3, 'alpha': int(alpha), 'share': round(a, 4),
            'delta': tuple(round(a * (f - b), 2) for f, b in zip(f3, bg3))}


def image_tone_glass_solve(bg, delta, alpha) -> dict:
    """
    Обратный контракт: заливка F по измеренной дельте и альфе, F = bg + delta / a.

    Нужен, когда дельта карточки уже известна с оригинала, а палитру клона
    пишут под неё: померил дельту — получил цвет пластины под выбранную альфу.
    Округление до целых 0..255; канал, вышедший за границы, лежит в
    'clipped' — значит альфа мала для такой дельты, цвет вышел бы «за белый».

    Args:
        bg: (r, g, b) — фон под пластиной.
        delta: (d0, d1, d2) — нужная дельта карточки относительно фона.
        alpha: альфа пластины, 0..255.

    Returns:
        {'bg', 'delta', 'alpha', 'share', 'fill': (r, g, b) — целые 0..255,
         'clipped': вышедшие за границы каналы, 'delta_check' — контракт
         пересчитанным fill'ом (что осталось после округления)}.
    """
    a = _glass_alpha(alpha)
    bg3 = _rgb3(bg, 'bg')
    d3 = tuple(float(v) for v in delta)
    if len(d3) != 3:
        raise ValueError(f'delta — три числа, получено {delta!r}')
    raw = [b + d / a for b, d in zip(bg3, d3)]
    fill = tuple(min(255, max(0, int(round(v)))) for v in raw)
    clipped = tuple(name for name, v in zip('rgb', raw) if not 0 <= v <= 255)
    return {'bg': bg3, 'delta': d3, 'alpha': int(alpha), 'share': round(a, 4),
            'fill': fill, 'clipped': clipped,
            'delta_check': image_tone_glass_contract(bg3, fill, alpha)['delta']}


def image_tone_glass_verify(path, y, bg, card, fill, alpha,
                            tol=IMAGE_TONE_GLASS_TOL, height=1) -> dict:
    """
    Проверка клона живым снимком: измеренную дельту строки — против контракта.

    Ожидаемое считают по фону того же снимка (`bg`-окно), поэтому проверка не
    спотыкается об градиент обоев и не требует, чтобы фон на экране совпал с
    эталонным цветом фон. `ok` — максимальная невязка по каналам не больше
    `tol` единиц.

    Args:
        path, y, bg, card, height: как в `image_tone_row_delta`.
        fill: (r, g, b) — заливка пластины, с которой пекли.
        alpha: её альфа, 0..255.
        tol: допуск невязки на канал, `IMAGE_TONE_GLASS_TOL`.

    Returns:
        {'file', 'y', 'measured', 'expected', 'residual', 'ok',
         'bg_rgb', 'card_rgb'}; expected и residual — вещественные.
    """
    m = image_tone_row_delta(path, y, bg, card, height=height)
    a = _glass_alpha(alpha)
    f3 = _rgb3(fill, 'fill')
    expected = tuple(round(a * (f - b), 2) for f, b in zip(f3, m['bg_rgb']))
    residual = tuple(round(md - e, 2) for md, e in zip(m['delta'], expected))
    return {'file': path, 'y': m['y'], 'bg_rgb': m['bg_rgb'], 'card_rgb': m['card_rgb'],
            'measured': m['delta'], 'expected': expected, 'residual': residual,
            'alpha': int(alpha), 'ok': max(abs(r) for r in residual) <= tol}


def _rgb_mean(img, y, height, x0, x1) -> tuple:
    """Средний цвет полосы [y, y+height) × [x0, x1), округлённый до целого."""
    vals = img[y:y + height, x0:x1].mean(axis=(0, 1))
    return tuple(int(round(float(v))) for v in vals)


def _glass_alpha(alpha) -> float:
    """Альфа 0..255 в долю; за границы не выпускать — делить на ноль некогда."""
    a = float(alpha)
    if not (0 < a <= 255):
        raise ValueError(f'alpha — целое 1..255 (как лежит в webp), получено {alpha!r}')
    return a / 255.0


def _rgb3(value, name) -> tuple:
    """Три целых канала из кортежа цвета (альфа, если четвёртая, отбрасывается)."""
    parts = tuple(int(v) for v in value)
    if len(parts) not in (3, 4):
        raise ValueError(f'{name} — (r, g, b) или (r, g, b, a), получено {value!r}')
    return parts[:3]


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
                    'вердикт «полоса или плавный градиент»; дельта карточки и '
                    'кромки в одной строке, стеклянный контракт заливки.')
    ap.add_argument('command', choices=['rows', 'band', 'delta', 'edge', 'glass'])
    ap.add_argument('path', nargs='?', default='',
                    help='картинка или снимок экрана (не нужен для glass)')
    ap.add_argument('--rect', default='', help='окно замера x0,y0,x1,y1; пусто — весь кадр')
    ap.add_argument('--chan', default='g-b', help="разность каналов: 'g-b', 'r-b' …")
    ap.add_argument('--min-luma', type=int, default=IMAGE_TONE_MIN_LUMA,
                    help='порог яркого пикселя, минимум по каналам')
    ap.add_argument('--step', type=int, default=IMAGE_TONE_STEP,
                    help='прореживание выборки по x')
    ap.add_argument('--min-jump', type=int, default=IMAGE_TONE_MIN_JUMP,
                    help='band: какой скачок считать полосой')
    ap.add_argument('--y', type=int, default=None, help='delta/edge: строка замера')
    ap.add_argument('--x', type=int, default=None, help='edge: первая колонка кромки')
    ap.add_argument('--w', type=int, default=3, help='edge: ширина зон кромки, столбцов')
    ap.add_argument('--bg', default='', help='delta: окно фона x0,x1; glass: цвет фона r,g,b')
    ap.add_argument('--card', default='', help='delta: окно карточки x0,x1')
    ap.add_argument('--probe', default='', help='edge: глубокое окно панели x0,x1')
    ap.add_argument('--height', type=int, default=1, help='delta/edge: высота полосы, строк')
    ap.add_argument('--fill', default='', help='glass: заливка r,g,b[,a]')
    ap.add_argument('--alpha', type=int, default=None, help='glass: альфа 0..255, по умолчанию из fill')
    ap.add_argument('--delta', default='',
                    help='glass: решить обратную задачу — найти fill по дельте d0,d1,d2 '
                         '(отрицательные — писать через «=»: --delta=-7,-7,-7)')
    ns = ap.parse_args()
    rect = tuple(int(v) for v in ns.rect.split(',')) if ns.rect else None

    def _need_path():
        if not ns.path:
            raise SystemExit(f'ошибка: {ns.command}: нужен снимок или картинка')

    def _win(raw, name, shape):
        parts = tuple(int(v) for v in raw.split(',')) if raw else None
        if parts is not None and len(parts) != len(shape.split(',')):
            raise SystemExit(f'ошибка: {name}: ждём {shape}, получено {raw!r}')
        return parts

    try:
        if ns.command == 'rows':
            _need_path()
            res = image_tone_rows(ns.path, rect=rect, chan=ns.chan,
                                  min_luma=ns.min_luma, step=ns.step)
            stride = max(1, len(res['rows']) // 80)
            print(f'{ns.path}: профиль {ns.chan}, окно {res["rect"]}, '
                  f'строк {res["count"]}')
            for y, v in res['rows'][::stride]:
                print(f'  y={y:>5} ' + ('—' if v is None else f'{v:+d}'))
        elif ns.command == 'band':
            _need_path()
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
        elif ns.command == 'delta':
            _need_path()
            if ns.y is None:
                raise SystemExit('ошибка: delta: нужен --y')
            res = image_tone_row_delta(ns.path, ns.y,
                                       _win(ns.bg, '--bg', 'x0,x1'),
                                       _win(ns.card, '--card', 'x0,x1'),
                                       height=ns.height)
            print(f'{ns.path}: y={res["y"]} фон{res["bg"]}={res["bg_rgb"]} '
                  f'карточка{res["card"]}={res["card_rgb"]} дельта {res["delta"]}')
        elif ns.command == 'edge':
            _need_path()
            if ns.y is None or ns.x is None:
                raise SystemExit('ошибка: edge: нужны --y и --x')
            res = image_tone_edge(ns.path, ns.y, ns.x, w=ns.w,
                                  probe=_win(ns.probe, '--probe', 'x0,x1'),
                                  height=ns.height)
            ref = 'probe' if res['probe_rgb'] else 'после'
            print(f'{ns.path}: y={res["y"]} x={res["x"]} w={res["w"]} — '
                  f'до {res["before_rgb"]}, кромка {res["edge_rgb"]}, '
                  f'после {res["after_rgb"]}; дельта по {ref} {res["delta"]}')
        elif ns.command == 'glass':
            fill = tuple(int(v) for v in ns.fill.split(',')) if ns.fill else None
            if fill and len(fill) not in (3, 4):
                raise SystemExit(f'ошибка: glass: нужен --fill r,g,b[,a], получено {ns.fill!r}')
            alpha = ns.alpha if ns.alpha is not None else (fill[3] if fill and len(fill) == 4 else None)
            if ns.path and ns.card:  # верификация живым снимком: bg/card — окна x0,x1
                if ns.y is None:
                    raise SystemExit('ошибка: glass (verify): нужен --y')
                if not fill or len(fill) < 3:
                    raise SystemExit('ошибка: glass (verify): нужен --fill r,g,b[,a]')
                if alpha is None:
                    raise SystemExit('ошибка: glass: альфа — четвёртым в --fill или через --alpha')
                res = image_tone_glass_verify(ns.path, ns.y,
                                              _win(ns.bg, '--bg', 'x0,x1'),
                                              _win(ns.card, '--card', 'x0,x1'),
                                              fill[:3], alpha, height=ns.height)
                print(f'{ns.path}: y={res["y"]} измерено {res["measured"]}, '
                      f'ожидано {res["expected"]}, невязка {res["residual"]} — '
                      f'{"OK" if res["ok"] else "НЕ в допуске"}')
            elif ns.delta:  # fill не нужен: solve его и решает
                bg3 = _win(ns.bg, '--bg', 'r,g,b')
                if not bg3:
                    raise SystemExit('ошибка: glass: для решения нужен --bg r,g,b')
                if alpha is None:
                    raise SystemExit('ошибка: glass: для решения нужна --alpha')
                d3 = _win(ns.delta, '--delta', 'd0,d1,d2')
                res = image_tone_glass_solve(bg3, d3, alpha)
                print(f'glass: fill {res["fill"]} под дельту {d3} при альфе {alpha}/255 '
                      f'(share {res["share"]}); пересчёт {res["delta_check"]}'
                      + (f'; КАНАЛЫ ЗА ГРАНИЦЕЙ: {res["clipped"]}' if res['clipped'] else ''))
            else:
                if not fill or len(fill) < 3:
                    raise SystemExit('ошибка: glass: ждём --fill r,g,b[,a]')
                if alpha is None:
                    raise SystemExit('ошибка: glass: альфа — четвёртым в --fill или через --alpha')
                bg3 = _win(ns.bg, '--bg', 'r,g,b')
                if not bg3:
                    raise SystemExit('ошибка: glass: ждём --bg r,g,b (или --delta для решения)')
                res = image_tone_glass_contract(bg3, fill[:3], alpha)
                print(f'glass: bg {res["bg"]} fill {res["fill"]} альфа {alpha}/255 '
                      f'(share {res["share"]}) → дельта {res["delta"]}')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')


if __name__ == '__main__':
    main()

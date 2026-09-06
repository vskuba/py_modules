"""
Изображения: замер фона, тоновая подгонка по эталону, аудит окон, проверка швов.

Отвечает на вопросы, которыми сверяют пачку картинок между собой и картинку
с тем, что нарисовано поверх неё (CSS-подложки, крышки запечённого текста):
«насколько ярким вышел фон» (`image_measure`), «сделай так же, как на эталоне»
(`image_match`), «не вводит ли окно замера в заблуждение — одинаковый ли фон
по всему кадру» (`image_audit`) и «виден ли стык на границе двух областей»
(`image_seam`). Фон ищется модой — самой частной краской окна: текста в окне
заведомо меньше, чем подложки, а среднее по кадру уезжает вперемешку с белыми
карточками.

Правка — кусочно-линейные уровни (levels): точка фона переезжает на
целевую яркость, чёрный закреплён в нуле, белый — в 255. Контраст тёмного
текста по фону при этом сохраняется, меняется только «прогрев» подложки.
Профиль идемпотентен лишь относительно оригинала, поэтому `image_match`
с `backup` правит всегда от нетронутой копии: повторный прогон повторяет
первый, а не складывает яркость дважды. Грабли замера — `image_tooling.md`.
"""
from collections import Counter
from pathlib import Path

from PIL import Image

# Pillow по умолчанию пишет JPEG с качеством 75 — на цельноэкранной графике
# это заметные артефакты вокруг текста; для правки фонов хватает 95.
IMAGE_JPEG_QUALITY = 95

# Разница яркостей зоны и медианы зон, с которой зона считается «не как все»:
# глаз замечает соседние области фона в ~6 ступеней Rec.601.
IMAGE_AUDIT_FLAG = 6

# Тонкие кромки кадра для аудита: проверяют, совпадает ли край с фоном —
# именно у края обычно берут окно замера. Отступ 2% — чтобы не захватывать
# скруглённые углы карточек.
IMAGE_AUDIT_EDGES = {
    'top': (0.02, 0.005, 0.98, 0.035),
    'bottom': (0.02, 0.965, 0.98, 0.995),
    'left': (0.005, 0.02, 0.035, 0.98),
    'right': (0.965, 0.02, 0.995, 0.98),
}

# Прыжок яркости между соседними пикселями профиля, с которого граница двух
# областей читается глазом как край (ступенька), а не как плавный переход.
IMAGE_SEAM_STEP = 6

# Окно вокруг границы, в пределах которого ищется ступенька вердикта: шов —
# это скачок на границе, а попавшие в длинный профиль буквы считать краем
# нельзя (профиль с ними просто показывают для отладки).
IMAGE_SEAM_WINDOW = 3


def image_measure(path: str, box: tuple = None, top: int = 0) -> dict:
    """
    Замерить фон картинки: моду цвета и её яркость.

    Args:
        path: файл картинки.
        box: окно замера `(x0, y0, x1, y1)` долями кадра, чтобы не зависеть
            от размера; пусто — весь кадр. Полоской у верхнего края
            (например, `(0.55, 0.015, 0.95, 0.06)`) берут полосу, где
            заведомо нет текста заголовка. Перед правкой по окну его
            репрезентативность проверяют `image_audit` — см. `image_tooling.md`.
        top: сколько верхних красок окна вернуть в ключе 'top' (0 — не считать);
            отвечает на «чем вообще заполнена эта область».

    Returns:
        {'mode': (r,g,b) самой частой краски, 'luma': её яркость 0–255,
         'mean_luma': средняя по окну, 'spread': p95−p5 яркости по окну
         (разброс: у однородного окна единицы), 'samples': число замеренных
         пикселей}; при top>0 добавлен 'top': [(краска, сколько раз)].
    """
    pixels = _window(_load(path), box)
    mode = Counter(pixels).most_common(1)[0][0]
    lumas = sorted(_luma(p) for p in pixels)
    result = {'mode': mode, 'luma': _luma(mode),
              'mean_luma': round(sum(lumas) / len(lumas)),
              'spread': lumas[int(0.95 * (len(lumas) - 1))] - lumas[int(0.05 * (len(lumas) - 1))],
              'samples': len(pixels)}
    if top > 0:
        result['top'] = Counter(pixels).most_common(top)
    return result


def image_audit(path: str, grid: tuple = (3, 3)) -> dict:
    """
    Аудит фона по зонам кадра: одинаковый ли он везде и не врёт ли окно замера.

    Историческая грабля: полоса «без текста у верхнего края» на одних картинках
    и есть фон, а на других попадает на светлую шапку — и подгонка по ней
    уводит остальной кадр в другую яркость (на card_front «Резерв+» шапка
    оказалась на 10 ступеней светлее лица). Треть кадра для шапки слишком
    крупна — мода зоны её не замечает, — поэтому кроме зон проверяются ещё
    тонкие кромки по всем четырём краям кадра: окно замера почти всегда
    берётся где-то у края, и кромка отвечает, совпадает ли он с фоном кадра.

    Args:
        path: файл картинки.
        grid: разбивка `(столбцы, строки)`; по умолчанию 3x3 — «трети»,
            как в правиле третей композиции.

    Returns:
        {'zones': [{'box': (x0,y0,x1,y1) долями, 'luma', 'mean_luma',
                    'spread'}] — по зонам слева-направо сверху-вниз,
         'edges': {'top'/'bottom'/'left'/'right': {'box', 'luma', 'spread'}},
         'tones': [[яркость, [где найдена]]] — сгруппированные фоны кадра,
         от самого частого,
         'warnings': [] при едином фоне кадра, иначе — сколько их и где
         каждый живёт: подгонку ведут окном внутри той области,
         с которой сверяются.
    """
    img = _load(path)
    cols, rows = grid
    # Без проверки нулевая разбивка не падает, а тихо отдаёт аудит без зон:
    # range(0) просто не крутится, и вердикт выносится по одним кромкам.
    if cols < 1 or rows < 1:
        raise ValueError(f'grid: ждём хотя бы 1x1, пришло {cols}x{rows}')
    zones = []
    for r in range(rows):
        for c in range(cols):
            box = (c / cols, r / rows, (c + 1) / cols, (r + 1) / rows)
            pixels = _window(img, box)
            lumas = sorted(_luma(p) for p in pixels)
            zones.append({'box': tuple(round(v, 3) for v in box),
                          'luma': _luma(Counter(pixels).most_common(1)[0][0]),
                          'mean_luma': round(sum(lumas) / len(lumas)),
                          'spread': lumas[int(0.95 * (len(lumas) - 1))] - lumas[int(0.05 * (len(lumas) - 1))]})
    edges = {}
    for name, box in IMAGE_AUDIT_EDGES.items():
        pixels = _window(img, box)
        edges[name] = {'box': tuple(round(v, 3) for v in box),
                       'luma': _luma(Counter(pixels).most_common(1)[0][0]),
                       'spread': _spread(pixels)}
    samples = ([ (z['luma'], f'зона {i + 1}') for i, z in enumerate(zones) ] +
               [ (e['luma'], _EDGE_NAMES[name]) for name, e in edges.items() ])
    tones = _tones(samples)
    warnings = []
    if len(tones) > 1:
        detail = '; '.join(f"L={t[0]} — {len(t[1])}: {', '.join(t[1])}" for t in tones)
        warnings.append(f'фон кадра не единый ({detail}) — '
                        f'окно замера бери внутри той области, под которую подгоняешь, а не «сверху у края»')
    return {'zones': zones, 'edges': edges, 'tones': tones, 'warnings': warnings}


def image_match(path: str, target, out: str = '', backup: str = '',
                box: tuple = None) -> dict:
    """
    Подогнать яркость фона картинки под эталон кусочно-линейными уровнями.

    Точка фона переезжает на яркость эталона; чёрный и белый закреплены,
    поэтому текст не сливается с фоном, а света не клиппуется.
    Альфа-канал RGBA сохраняется как есть — уровни гоняются только по RGB;
    на JPEG альфы не бывает, и при записи в JPEG она теряется (как и теряется
    прозрачность палитровых P с tRNS — они, как и раньше, сводятся к RGB).

    Args:
        path: правая картинка.
        target: эталон — путь к картинке (целью станет яркость её фона)
            или готовая яркость 0–255 числом.
        out: куда писать; пусто — поверх `path`.
        backup: каталог нетронутых оригиналов; правка идёт от них, а не от
            текущего состояния файла. Нет копии — она заводится первым
            делом; второй прогон тогда повторяет первый, а не складывает
            яркость дважды.
        box: окно замера фона, как в `image_measure`.

    Returns:
        {'file': куда записано, 'before': яркость фона до, 'after': после
         (пересчитана с записанного файла), 'target': целевая яркость,
         'changed': был ли записан файл}.
    """
    src = Path(path)
    if backup:
        src = Path(backup) / Path(path).name
        if not src.exists():
            Path(backup).mkdir(parents=True, exist_ok=True)
            src.write_bytes(Path(path).read_bytes())
    dst = Path(out) if out else Path(path)

    want = target if isinstance(target, int) else image_measure(str(target), box=box)['luma']
    got = image_measure(str(src), box=box)['luma']
    if abs(got - want) <= 1 or not 0 < got < 255:
        return {'file': str(dst), 'before': got, 'after': got,
                'target': want, 'changed': False}

    image, alpha = _split_alpha(Image.open(src))
    image = image.point(_levels_lut(got, want))
    if alpha is not None and dst.suffix.lower() not in ('.jpg', '.jpeg'):
        image = Image.merge('RGBA', (*image.split(), alpha))
    if dst.suffix.lower() in ('.jpg', '.jpeg'):
        image.convert('RGB').save(dst, quality=IMAGE_JPEG_QUALITY)
    else:
        image.save(dst)
    after = image_measure(str(dst), box=box)
    return {'file': str(dst), 'before': got, 'after': after['luma'],
            'target': want, 'changed': True}


def image_seam(path: str, pos: int, at: int = None, axis: str = 'x',
               span: int = 24, band: int = 6) -> dict:
    """
    Проверить шов: одно ли это фон по обе стороны границы `pos` или виден край.

    Им нужен там, где на картинку/снимок наложен плоский слой (CSS-подложка,
    крышка запечённого текста): глаз читает пятном разницу уже в ~6 ступеней,
    а «внутри/снаружи» руками по пикселю не меряют — профиль считает здесь.
    Профиль — медианная яркость тонкой перпендикулярной полосы, идущей через
    границу; медиана, чтобы одна тёмная буква не кричала «шов».

    Args:
        path: картинка или снимок экрана (физические пиксели).
        pos: координата границы по оси (`x` — вертикальная линия, `y` —
            горизонтальная), пиксели кадра.
        at: перпендикулярная координата, на которой мерить полосу;
            пусто — центр кадра. Полоса не должна пересекать подписи и рамки:
            их ступени останутся в профиле, но вердикт смотрит только
            вблизи границы (IMAGE_SEAM_WINDOW).
        axis: 'x' или 'y'.
        span: ширина каждой из сравниваемых полос вдоль оси.
        band: толщина профильной полосы поперёк оси; выбирать место без текста.

    Returns:
        {'a': яркость полосы до границы, 'b': после, 'delta': a−b,
         'max_step': самый большой скачок между соседними пикселями
         вблизи границы — он решает вердикт, 'edge': True, если он
         ≥ IMAGE_SEAM_STEP; для отладки — 'profile_max' по всему профилю
         и сам 'profile' (яркости от pos−span до pos+span)}.
    """
    img = _load(path)
    w, h = img.size
    if axis not in ('x', 'y'):
        raise ValueError(f"axis: ждём 'x' или 'y', пришло {axis!r}")
    pos = int(pos)
    along, across = (w, h) if axis == 'x' else (h, w)
    if not 0 < pos < along:
        raise ValueError(f'pos={pos} вне кадра по оси {axis} (0..{along})')
    mid = min(max(0, int(at) if at is not None else across // 2), across - 1)
    lo, hi = max(0, mid - band // 2), min(across, mid + band // 2 + 1)
    start, stop = max(0, pos - span), min(along, pos + span)
    if axis == 'x':
        profile = [_median_luma(img.crop((i, lo, i + 1, hi))) for i in range(start, stop)]
    else:
        profile = [_median_luma(img.crop((lo, i, hi, i + 1))) for i in range(start, stop)]
    k = pos - start  # сколько точек профиля до границы
    steps = [abs(y - x) for x, y in zip(profile, profile[1:])]
    near = [s for i, s in enumerate(steps)
            if pos - IMAGE_SEAM_WINDOW <= start + i < pos + IMAGE_SEAM_WINDOW]
    max_step = max(near, default=0)
    return {'a': _median_luma_profile(profile[:k]), 'b': _median_luma_profile(profile[k:]),
            'delta': _median_luma_profile(profile[:k]) - _median_luma_profile(profile[k:]),
            'max_step': max_step, 'profile_max': max(steps, default=0),
            'profile': profile, 'edge': max_step >= IMAGE_SEAM_STEP}


def _load(path: str) -> Image.Image:
    """Картинка в RGB: режим P и альфа замерам не нужны, а в LUT лягут криво."""
    return Image.open(path).convert('RGB')


def _split_alpha(image: Image.Image) -> tuple:
    """(RGB-слои, альфа-канал или None): уровни гоняются по RGB, прозрачность — в обход."""
    if 'A' in image.mode:
        image = image.convert('RGBA')
        r, g, b, a = image.split()
        return Image.merge('RGB', (r, g, b)), a
    return image.convert('RGB'), None


def _window(img: Image.Image, box: tuple = None) -> list:
    """
    Пиксели окна (доли кадра) списком RGB-кортежей.

    Вырожденное окно — отказ, а не пустой список. Пустой уезжает вглубь,
    к `Counter(...).most_common(1)[0]`, и возвращается оттуда `IndexError:
    list index out of range` — по нему не видно ни кадра, ни окна. Так
    ломался `image_audit` на мелкой картинке: доли кромки (0.005–0.035)
    после `int()` схлопывались в ноль пикселей.
    """
    if box:
        w, h = img.size
        x0, y0, x1, y1 = box
        crop = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
        if crop[2] <= crop[0] or crop[3] <= crop[1]:
            raise ValueError(
                f'окно {tuple(round(v, 3) for v in box)} на кадре {w}x{h} — '
                f'это {crop[2] - crop[0]}x{crop[3] - crop[1]} px: мерить нечего')
        img = img.crop(crop)
    # Pillow 14 объявил getdata устаревшим в пользу get_flattened_data,
    # а в старых версиях нового имени нет — берём то, что есть.
    if hasattr(img, 'get_flattened_data'):
        return list(img.get_flattened_data())
    return list(img.getdata())


def _levels_lut(bg: int, target: int) -> tuple:
    """
    Палитра уровней для `Image.point`: якоря 0 → 0, фон → цель, 255 → 255,
    одна и та же кривая на каждый канал. `Image.point` на RGB просит ровно
    768 записей — 256, повторённые трижды, иначе «wrong number of lut
    entries».
    """
    low = [round(v * target / bg) for v in range(bg)]
    high = [round(target + (v - bg) * (255 - target) / (255 - bg)) for v in range(bg, 256)]
    return tuple((low + high) * 3)


def _luma(rgb: tuple) -> int:
    """Яркость Rec.601 — то же восприятие, что у глаз."""
    r, g, b = rgb
    return round(0.299 * r + 0.587 * g + 0.114 * b)


def _median_luma(img: Image.Image) -> int:
    """Медианная яркость области: устойчива к одиночным пикселям текста."""
    return _median_luma_profile([_luma(p) for p in _window(img)])


def _median_luma_profile(lumas: list) -> int:
    """Медиана уже посчитанных яркостей (пустой список — 0)."""
    if not lumas:
        return 0
    lumas = sorted(lumas)
    return lumas[len(lumas) // 2]


def _spread(pixels: list) -> int:
    """Разброс яркости окна: p95 − p5; у однородной области — единицы."""
    lumas = sorted(_luma(p) for p in pixels)
    return lumas[int(0.95 * (len(lumas) - 1))] - lumas[int(0.05 * (len(lumas) - 1))]


_EDGE_NAMES = {'top': 'кромка сверху', 'bottom': 'кромка снизу',
               'left': 'кромка слева', 'right': 'кромка справа'}


def _tones(samples: list) -> list:
    """
    Сгруппировать промеры (яркость, имя) по фонам: соседние по яркости
    (после сортировки) в пределах IMAGE_AUDIT_FLAG — один фон. Возвращает
    [[яркость, [имена]], ...] от самого частого фона. Медиана на картинке
    с двумя фонами (страница и карточка) вводит в заблуждение, а группы —
    честны: они показывают, что фон не один и где какой лежит.
    """
    groups = []
    for luma, name in sorted(samples):
        if groups and luma - groups[-1][-1][0] <= IMAGE_AUDIT_FLAG:
            groups[-1].append((luma, name))
        else:
            groups.append([(luma, name)])
    tones = [[round(sum(l for l, _ in g) / len(g)), [n for _, n in g]] for g in groups]
    tones.sort(key=lambda t: (-len(t[1]), t[0]))
    return tones


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Замер яркости фона, тоновая подгонка по эталону, аудит окон, проверка швов.')
    parser.add_argument('command', choices=['measure', 'match', 'audit', 'seam'])
    parser.add_argument('files', nargs='+', help='картинки (для seam — одна)')
    parser.add_argument('--target', default='', help='эталон для match: файл или яркость 0-255')
    parser.add_argument('--out', default='', help='каталог для match; пусто — поверх')
    parser.add_argument('--backup', default='', help='каталог нетронутых оригиналов (идемпотентность)')
    parser.add_argument('--box', default='', help='окно замера x0,y0,x1,y1 долями кадра')
    parser.add_argument('--top', type=int, default=0, help='measure: сколько верхних красок показать')
    parser.add_argument('--pos', type=int, default=None, help='seam: координата границы по оси')
    parser.add_argument('--at', type=int, default=None, help='seam: перпендикулярная координата полосы')
    parser.add_argument('--axis', default='x', choices=['x', 'y'], help='seam: ось границы')
    parser.add_argument('--span', type=int, default=24, help='seam: ширина полос вдоль оси')
    parser.add_argument('--band', type=int, default=6, help='seam: толщина профиля поперёк оси')
    ns = parser.parse_args()
    box = tuple(float(v) for v in ns.box.split(',')) if ns.box else None

    try:
        if ns.command == 'measure':
            for f in ns.files:
                m = image_measure(f, box=box, top=ns.top)
                line = (f'{f}: фон {m["mode"]} L={m["luma"]} (средняя {m["mean_luma"]}, '
                        f'разброс {m["spread"]}, {m["samples"]} px)')
                if ns.top > 0:
                    line += '\n  ' + '; '.join(f'{c}×{n}' for c, n in m['top'])
                print(line)
        elif ns.command == 'audit':
            for f in ns.files:
                a = image_audit(f)
                print(f'{f}: ' + '; '.join(f'L={t[0]}×{len(t[1])}' for t in a['tones']))
                for i, z in enumerate(a['zones']):
                    print(f'  зона {i + 1} {z["box"]}: L={z["luma"]:>3} '
                          f'(средняя {z["mean_luma"]}, разброс {z["spread"]})')
                for name, e in a['edges'].items():
                    print(f'  {_EDGE_NAMES[name]}: L={e["luma"]:>3} (разброс {e["spread"]})')
                for w in a['warnings']:
                    print(f'  ВНИМАНИЕ: {w}')
        elif ns.command == 'seam':
            if ns.pos is None:
                raise SystemExit('нужен --pos: координата границы, по которой проверяем стык')
            for f in ns.files:
                s = image_seam(f, ns.pos, at=ns.at, axis=ns.axis, span=ns.span, band=ns.band)
                verdict = (f'край виден: ступень {s["max_step"]}' if s['edge']
                           else 'стыка не видно')
                print(f'{f}: {ns.axis}={ns.pos} — до {s["a"]}, после {s["b"]} '
                      f'(Δ {s["delta"]}, max_step {s["max_step"]}) — {verdict}')
        else:
            if not ns.target:
                raise SystemExit('нужен --target: файл эталона или яркость числом')
            target = int(ns.target) if ns.target.isdigit() else ns.target
            for f in ns.files:
                out = str(Path(ns.out) / Path(f).name) if ns.out else f
                r = image_match(f, target, out=out, backup=ns.backup, box=box)
                print(f'{f}: {r["before"]} → {r["after"]} (цель {r["target"]}, '
                      f'{"записано" if r["changed"] else "уже в допуске"}) → {r["file"]}')
    except (OSError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

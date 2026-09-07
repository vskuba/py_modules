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

Тонкие линии и разметка — свои вопросы: «где рамка, сколько её пикселей каким
цветом, какие углы» (`image_stroke`), «каким будет этот цвет на тонированном
кадре» (`image_expose`), «цел ли тоновый контракт hex-литералов разметки»
(`image_contract`) и «какая это доля кадра, с вычетом полей» (`image_frac`).

Правка — кусочно-линейные уровни (levels): точка фона переезжает на
целевую яркость, чёрный закреплён в нуле, белый — в 255. Контраст тёмного
текста по фону при этом сохраняется, меняется только «прогрев» подложки.
Профиль идемпотентен лишь относительно оригинала, поэтому `image_match`
с `backup` правит всегда от нетронутой копии: повторный прогон повторяет
первый, а не складывает яркость дважды. Грабли замера — `image_tooling.md`.
"""
import re
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

# Сколько положений полосы перпендикулярно оси перебирает поиск спокойного
# места (`best`): полоса ищется без глифов, потому что на тексте ступень
# завышается и вердикт врал бы. Шаг берётся не меньше толщины полосы — иначе
# соседние пробы меряли бы одно и то же, и «спокойно» засчитывалось бы трижды.
IMAGE_SEAM_SCAN = 96

# Цветовые литералы разметки: hex (#rgb/#rgba/#rrggbb/#rrggbbaa) и rgb()/rgba();
# альфа у hex отбрасывается, функциональные сводятся к тем же '#rrggbb'.
# Именованных цветов и дробных значений тут намеренно нет: контракт тону
# держится на hex-литералах, снятых с картинки.
_LITERAL_RE = re.compile(r'#([0-9a-fA-F]{3,8})\b|rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)', re.IGNORECASE)

# Отклонение яркости пикселя от местного фона, с которого он принадлежит
# тонкой линии: глаз держит 3-px штрих по контрасту десятков ступеней,
# AA-переход слабее — в ядро штриха он не входит.
IMAGE_STROKE_CONTRAST = 12

# Сколько пикселей вокруг данной стороны прямоугольника искать линию. Заодно
# потолок радиуса, который ещё видно: дуга дальше pad'а с экрана не читается,
# и оценка радиуса выше pad'а не гарантирована.
IMAGE_STROKE_PAD = 24

# Толщина профилирующей полосы вдоль стороны: сечение линии ищут медианой
# поперёк полосы — одиночные пиксели текста или шума не должны двигать центр.
IMAGE_STROKE_BAND = 4

# Отклонение канала литерала от измеренной моды, в котором тон считается
# совпавшим: JPEG-шум пачки не должен объявлять дрейф на ровном месте.
IMAGE_CONTRACT_TOL = 3

# Максимальный зазор (в символах) между литералом и комментарием-атрибуцией:
# атрибуцией признаёт ближайший комментарий, но не первый комментарий файла —
# иначе одно пояснение в начале CSS оправдало бы все литералы разметки.
IMAGE_CONTRACT_GAP = 300

# Атрибуция тонового контракта в комментарии: картинка-источник и окно замера
# (`card_front.jpg (0.05,0.30,0.95,0.62)`). Без окна атрибуции нет: «с картинки»
# без окна неперепроверяемо.
_ATTR_FILE_RE = re.compile(r'[\w.\-]*[\w\-]\.(?:png|jpe?g|webp)\b', re.IGNORECASE)
_ATTR_BOX_RE = re.compile(r'\(\s*(\d*\.?\d+)\s*,\s*(\d*\.?\d+)\s*,\s*(\d*\.?\d+)\s*,\s*(\d*\.?\d+)\s*\)')


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


def image_diff_zones(path_a: str, path_b: str, grid: tuple = (3, 3),
                     tol: int = IMAGE_AUDIT_FLAG) -> dict:
    """
    Сравнить два кадра зона-к-зоне: один ли тон там, где сверяют окно замера.

    Историческая грабля: «починили контраст» меряют двумя вызовами
    `image_measure` и вычитают числа в голове — так же легко вычесть
    не то окно, как и не так. Инструмент обходит сетку на двух кадрах
    сразу и вердикты отдаёт готовыми. Окна — доли кадра, поэтому кадром
    другой высоты (свой статус-бар, другой Android) не собьёт координатой,
    но и не сравнит: доли считают по каждому кадру свою арифметику.

    Args:
        path_a: первый кадр (например, эталонный скриншот).
        path_b: второй кадр (напр. снимок с эмулятора после правки).
        grid: разбивка `(столбцы, строки)`; по умолчанию 3x3, как в `image_audit`.
        tol: яркостная разница, с которой зона считается «не как в эталоне»
            (та же ступень восприятия, `IMAGE_AUDIT_FLAG`).

    Returns:
        {'zones': [{'box': (x0,y0,x1,y1) долями, 'a', 'b': яркости мод зон,
                    'delta': a−b, 'edge': abs(delta) ≥ tol}],
         'warnings': [] при едином тоне кадров, иначе — в каких зонах кадры
         различаются; окна — доли каждого кадра, арифметика vw→px не нужна.
    """
    img_a, img_b = _load(path_a), _load(path_b)
    cols, rows = grid
    if cols < 1 or rows < 1:
        raise ValueError(f'grid: ждём хотя бы 1x1, пришло {cols}x{rows}')
    zones = []
    for r in range(rows):
        for c in range(cols):
            box = (c / cols, r / rows, (c + 1) / cols, (r + 1) / rows)
            la = _luma(Counter(_window(img_a, box)).most_common(1)[0][0])
            lb = _luma(Counter(_window(img_b, box)).most_common(1)[0][0])
            zones.append({'box': tuple(round(v, 3) for v in box),
                          'a': la, 'b': lb, 'delta': la - lb,
                          'edge': abs(la - lb) >= tol})
    warnings = []
    diff = [i + 1 for i, z in enumerate(zones) if z['edge']]
    if diff:
        warnings.append(f'кадры различаются в {len(diff)} зонах: '
                        f'{", ".join(str(i) for i in diff)} — '
                        f'окно замера бери внутри той области, под которую подгоняешь')
    return {'a': path_a, 'b': path_b, 'zones': zones, 'warnings': warnings}


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
               span: int = 24, band: int = 6, best: bool = False) -> dict:
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
        best: не верить одному `at`, а перебрать полосу по всей длине (шагом
            `at` пренебречь) и решить по самой спокойной позиции: на глифах
            ступень завышается, и один неудачный `at` объявил бы шов там,
            где его нет.

    Returns:
        {'a': яркость полосы до границы, 'b': после, 'delta': a−b,
         'max_step': самый большой скачок между соседними пикселями
         вблизи границы — он решает вердикт, 'edge': True, если он
         ≥ IMAGE_SEAM_STEP; для отладки — 'profile_max' по всему профилю
         и сам 'profile' (яркости от pos−span до pos+span); при best
         добавлены 'at' — выбранная позиция и 'agree'/'scanned' — сколько
         проб дали тот же вердикт (стойкость: ступенька текста уходит,
         настоящая — нет).
    """
    img = _load(path)
    if axis not in ('x', 'y'):
        raise ValueError(f"axis: ждём 'x' или 'y', пришло {axis!r}")
    pos = int(pos)
    along, across = (img.size if axis == 'x' else (img.size[1], img.size[0]))
    if not 0 < pos < along:
        raise ValueError(f'pos={pos} вне кадра по оси {axis} (0..{along})')
    if not best:
        mid = min(max(0, int(at) if at is not None else across // 2), across - 1)
        return _seam_measure(img, pos, mid, axis, span, band)
    return _seam_scan(img, pos, axis, span, band)


def image_rect_seams(path: str, rect, tol: int = IMAGE_SEAM_STEP,
                     span: int = 24, band: int = 6) -> dict:
    """
    Проверить швы прямоугольника целиком: все четыре его края одним вызовом.

    Плоский CSS-слой (подложка, крышка запечённого текста) невидим только
    тогда, когда стык не читается ни с одной стороны; шов на одном краю —
    это уже пятно с видимым краем. Прямоугольник берут из
    `adb_cdp_element_rect` (запись `screen`): она знает экранные координаты
    элемента с поправкой на статус-бар, своей арифметики vw→px здесь нет.
    Полосу каждой стороны ищут спокойную, как `best` у `image_seam`: на
    глифах ступень завышается и ни за что объявила бы сторону швом.

    Args:
        path: картинка или снимок экрана (физические пиксели).
        rect: прямоугольник — словарь {'x','y','w','h'} (запись `screen`
            из `adb_cdp_element_rect`) или кортеж (x, y, w, h).
        tol: ступень яркости, с которой край читается глазом как край
            (по умолчанию `IMAGE_SEAM_STEP`).
        span: ширина сравниваемых полос вдоль стороны.
        band: толщина профильной полосы поперёк стороны.

    Returns:
        {'file': путь, 'rect': нормализованный {'x','y','w','h'},
         'ok': True, если ни один край не читается краем,
         'sides': {'left'/'right'/'top'/'bottom': {'at', 'a', 'b',
         'max_step', 'edge', 'agree'} — или {'skip': ...}, если сторона
         лежит ровно на краю кадра: сравнивать там не с чем}.
    """
    img = _load(path)
    w, h = img.size
    if isinstance(rect, dict):
        x, y = int(rect['x']), int(rect['y'])
        rw, rh = int(rect['w']), int(rect['h'])
    else:
        x, y, rw, rh = (int(v) for v in rect)
    if rw <= 0 or rh <= 0:
        raise ValueError(f'размер прямоугольника {rw}×{rh}: мерить нечего')
    sides = {}
    for name, (pos, axis, span0, span1) in {
            'left': (x, 'x', y, y + rh), 'right': (x + rw, 'x', y, y + rh),
            'top': (y, 'y', x, x + rw), 'bottom': (y + rh, 'y', x, x + rw)}.items():
        along = w if axis == 'x' else h
        if not 0 < pos < along:
            sides[name] = {'skip': 'сторона ровно по краю кадра — сравнивать не с чем'}
            continue
        across = h if axis == 'x' else w
        # полосу ищем внутри прямоугольника (с отступом 2 px от его углов):
        # за ним меряет уже не этот слой, а соседний.
        lo, hi = max(0, min(span0 + 2, across)), min(max(span1 - 2, 0), across)
        sides[name] = _seam_scan(img, pos, axis, span, band, lo, hi, tol=tol)
    return {'file': path, 'rect': {'x': x, 'y': y, 'w': rw, 'h': rh},
            'ok': all(not s.get('edge') for s in sides.values()), 'sides': sides}


def image_literals(path: str) -> dict:
    """
    Инвентарь цветов разметки: каждый литерал текста с его яркостью.

    Тоновый контракт обязывает после `image_match` перемерять все тоновые
    литералы CSS — до этого это grep по файлу и глаза: инвентарь выдаёт
    каждый уникальный цвет, его яркость Rec.601 и сколько раз он встретился,
    так что «что заявлено в CSS и не уехал ли тон» — одна ведомость.

    Args:
        path: HTML- или CSS-файл.

    Returns:
        {'file': путь, 'colors': [{'color': '#rrggbb', 'luma': 0–255,
         'count': сколько раз}]} по убыванию числа встреч; 4- и 8-значные
        литералы возвращаются без альфа-канала, `rgb()`/`rgba()` сведены
        к тем же '#rrggbb', что и hex.
    """
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    found = Counter()
    for match in _LITERAL_RE.finditer(text):
        body = _literal_hex(match)
        if body:
            found[body] += 1
    colors = [{'color': c,
               'luma': _luma((int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))),
               'count': n} for c, n in found.items()]
    colors.sort(key=lambda c: (-c['count'], c['luma'], c['color']))
    return {'file': path, 'colors': colors}


def image_stroke(path: str, rect=None, pad: int = IMAGE_STROKE_PAD,
                 contrast: int = IMAGE_STROKE_CONTRAST) -> dict:
    """
    Измерить тонкую обводку (рамку) по сторонам прямоугольника: положение,
    толщину ядра, цвет ядра и радиус скругления углов.

    Это измерительная половина задачи «дорисовать рамку»: seam отвечает на
    «видно ли стык», а тут — «где линия, сколько её пикселей каким цветом,
    какие углы». Сечение линии ищут вокруг данной стороны прямоугольника
    (запись `screen` из `adb_cdp_element_rect` или кортеж `x,y,w,h`; пусто —
    весь кадр): поперёк стороны — медианный профиль яркости, ядро — подряд
    идущие пиксели с отклонением от местного фона не меньше `contrast` вокруг
    самой тёмной точки. AA-переход в ядро не входит: глаз различает штрих
    ядром, а заплатку подгоняют по ядру.

    Радиус угла — по изгибу линии к углам: вдоль каждой стороны трассируют
    центр ядра; в дуге центр уходит внутрь прямоугольника. Радиус — не
    максимальный уход (у самого угла штрих стоит вертикально, медианного
    сечения там нет, и максимум недооценивает радиус вдвое), а посадка точек
    дуги обеих сторон угла на окружность (`_corner_radius`). Большие радиусы,
    упирающиеся в `pad`, видно не будут — потолок оценки равен pad'у.

    Args:
        path: картинка или снимок экрана (физические пиксели).
        rect: прямоугольник — словарь {'x','y','w','h'} (запись `screen`),
            кортеж (x, y, w, h) или None (весь кадр).
        pad: сколько пикселей вокруг стороны искать линию (и потолок радиуса).
        contrast: отклонение яркости от местного фона, принадлежащее штриху.

    Returns:
        {'file': путь, 'rect': нормализованный {'x','y','w','h'},
         'found': True, если линия нашлась у всех четырёх сторон,
         'sides': {'left'/'right'/'top'/'bottom':
            {'pos', 'offset' (pos − данной; у рамок, рисуемых внутрь
            номинального края — до половины ширины штриха), 'width', 'rgb',
            'luma'} — или {'none': ...}, если линии у стороны нет},
         'radius': радиус скругления (int) или None, 'css':
            {'border_vw', 'radius_vw'} — толщины долями ширины кадра ×100,
            теми самыми vw, которыми их рисуют в CSS}.
    """
    img = _load(path)
    w, h = img.size
    if rect is None:
        x, y, rw, rh = 0, 0, w, h
    elif isinstance(rect, dict):
        x, y, rw, rh = int(rect['x']), int(rect['y']), int(rect['w']), int(rect['h'])
    else:
        x, y, rw, rh = (int(v) for v in rect)
    if rw <= 0 or rh <= 0:
        raise ValueError(f'размер прямоугольника {rw}×{rh}: мерить нечего')
    sides, traces = {}, {}
    for name, (pos, axis, lo, hi) in {
            'left': (x, 'x', y, y + rh), 'right': (x + rw, 'x', y, y + rh),
            'top': (y, 'y', x, x + rw), 'bottom': (y + rh, 'y', x, x + rw)}.items():
        trace = []
        for mid in range(lo, max(lo + 1, hi), IMAGE_STROKE_BAND):
            got = _stroke_trace(img, pos, mid, axis, pad, contrast)
            if got:
                trace.append((mid, *got))
        sides[name], traces[name] = _stroke_side(pos, trace)
    found = all('none' not in s for s in sides.values())
    # inward — уход центра ядра внутрь прямоугольника в дуге; знак зависит от
    # того, какой стороной смотрит сторона кадра.
    inward = {}
    for name in ('left', 'top'):
        inward[name] = [(mid, center - sides[name].get('pos', 0), width)
                        for mid, center, width, _ in traces[name]]
    for name in ('right', 'bottom'):
        inward[name] = [(mid, sides[name].get('pos', 0) - center, width)
                        for mid, center, width, _ in traces[name]]
    # угол = посадка точек дуги на окружность с центром в (r, r) от угла;
    # места слияния с соседней стороной отфильтрованы по ширине ядра (там
    # дуга уже не дуга). lead — половина стороны ближе к левому/верхнему
    # концу её размаха, trail — к правому/нижнему.
    ranges = {'left': (y, y + rh), 'right': (y, y + rh),
              'top': (x, x + rw), 'bottom': (x, x + rw)}
    radius = None
    if all('width' in s for s in sides.values()):
        limit = min(s['width'] for s in sides.values()) + 2

        def arc_pts(side, lead):
            """Точки дуги у угла: (вдоль стороны от угла, inward-уход центра)."""
            lo, hi = ranges[side]
            mid_c = (lo + hi) / 2
            return [((mid - lo if lead else hi - mid), dev) if side in ('top', 'bottom')
                    else (dev, (mid - lo if lead else hi - mid))
                    for mid, dev, width in inward[side]
                    if width <= limit and 1 < dev <= pad and (lead == (mid < mid_c))]

        corner_r = [_corner_radius(arc_pts(side_a, a_lead) + arc_pts(side_b, b_lead), pad)
                    for side_a, a_lead, side_b, b_lead in (
                        ('top', True, 'left', True), ('top', False, 'right', True),
                        ('bottom', True, 'left', False),
                        ('bottom', False, 'right', False))]
        if corner_r:
            radius = round(_median_luma_profile(corner_r))
    css = None
    if found:
        css = {'border_vw': round(min(s['width'] for s in sides.values()) / w * 100, 2),
               'radius_vw': round(radius / w * 100, 2) if radius is not None else None}
    return {'file': path, 'rect': {'x': x, 'y': y, 'w': rw, 'h': rh},
            'found': found, 'sides': sides, 'radius': radius, 'css': css}


def image_expose(path_a: str, path_b: str, pairs: list, colors: list = None) -> dict:
    """
    Перенести тон между двумя кадрами одного компонента при разном
    «экспонировании»: цвет из кадра A перевести в тот же цвет кадра B.

    Сырой снимок и тонированная страница показывают одну и ту же деталь по-
    разному (страница 170 vs 214): снятый с сырого цвета CSS-литерал
    даёт тёмную рамку на подогнанной странице. `image_match` ровняет фон одного
    файла; здесь перенос произвольного цвета между двумя: по референсным
    окнам (одни и те же доли кадра в обоих) строится кусочно-линейная кривая
    на каждый канал с якорем 0 → 0 — как уровни `image_match`, только между
    двумя кадрами; по ней переводится всё, что спросят.

    Args:
        path_a: кадр-источник (например, сырой снимок).
        path_b: кадр-цель (например, тонированная страница).
        pairs: окна-референсы `(x0, y0, x1, y1)` долями кадра, одни и те же
            в обоих кадрах; краски в них должны различаться по яркости.
        colors: список (r,g,b), которые надо перенести; пусто — только ведомость.

    Returns:
        {'file_a', 'file_b', 'pairs': [{'box', 'a', 'b', 'a_luma', 'b_luma'}],
         'gains': (gr, gg, gb) — усиление по самой тёмной паре,
         'residual': максимальное отклонение пар от этого наклона (нелинейность
         переноса), 'mapped': [{'from', 'to'}] для запрошенных colors}.

    Raises:
        ValueError: окна пусты, референсные краски неразличимы или канал
            немонотонен (перенос не определён).
    """
    img_a, img_b = _load(path_a), _load(path_b)
    if not pairs:
        raise ValueError('без окон-референсов переносить не на чём')
    refs = []
    for box in pairs:
        a = Counter(_window(img_a, box)).most_common(1)[0][0]
        b = Counter(_window(img_b, box)).most_common(1)[0][0]
        refs.append({'box': tuple(box), 'a': a, 'b': b,
                     'a_luma': _luma(a), 'b_luma': _luma(b)})
    lumas = [r['a_luma'] for r in refs]
    if len(set(lumas)) != len(lumas):
        raise ValueError(f'референсные краски {lumas} неразличимы по яркости — '
                         'возьми окна разных красок')
    refs = sorted(refs, key=lambda r: r['a_luma'])
    luts = []
    for ch in range(3):
        xs = [0] + [r['a'][ch] for r in refs] + [255]
        ys = [0] + [r['b'][ch] for r in refs] + [255]
        for x, y in zip(xs, xs[1:]):
            if y <= x:
                raise ValueError(f'канал {ch}: перенос немонотонен ({xs} → {ys})')
        luts.append((xs, ys))
    # усиление — по самой тёмной паре: свет умножается, а яркие к свету B
    # упираются в потолок, и об этом говорит residual, а не перекос gain'а.
    base = refs[0]
    gains = tuple(round(base['b'][ch] / base['a'][ch], 3) if base['a'][ch] else 255.0
                  for ch in range(3))
    residual = max(abs(r['b'][ch] - round(gains[ch] * r['a'][ch]))
                   for r in refs for ch in range(3))
    mapped = [{'from': tuple(c),
               'to': tuple(_interp(*luts[ch], v) for ch, v in enumerate(c))}
              for c in (colors or [])]
    return {'file_a': path_a, 'file_b': path_b, 'pairs': refs,
            'gains': gains, 'residual': residual, 'mapped': mapped}


def image_contract(path: str, tol: int = IMAGE_CONTRACT_TOL,
                   gap: int = IMAGE_CONTRACT_GAP) -> dict:
    """
    Проверить тоновый контракт разметки: у каждого цветового литерала есть
    атрибуция «картинка + окно замера» в ближайшем комментарии, и тон
    литерала совпадает с измеренным по этому окну.

    Контракт («hex в CSS — с картинки, и рядом в комментарии файл и окно
    замера») до этого проверялся grep'ом и глазами. Инструмент читает HTML/CSS
    как текст и для каждого литерала (hex и rgb()/rgba(), литералы внутри
    комментариев не в счёт) ищет ближайший комментарий не дальше `gap`
    символов, в котором названа картинка с окном (`card_front.jpg
    (0.05,0.30,0.95,0.62)`), мерит моду этого окна и сверяет с литералом.
    Картинка ищется рядом с разметкой, в `public/images/` и `public/` под ней:
    там рукописные страницы держат свои картинки.

    Args:
        path: HTML- или CSS-файл.
        tol: максимальное отклонение канала, в котором тон считается совпавшим.
        gap: максимальный зазор между литералом и комментарием-атрибуцией.

    Returns:
        {'file': путь, 'tol': tol,
         'colors': [{'color', 'luma', 'line', 'status', 'file', 'box',
                     'measured', 'measured_luma'}] по строкам разметки,
         'summary': {'ok': n, 'drifted': n, 'unattributed': n, 'missing': n},
         'ok': True, если дрейфа, неатрибутированных и отсутствующих нет}.
        status: 'ok' | 'drifted' (тон уехал — картинку правили) |
        'unattributed' (нет комментария с картинкой и окном) | 'missing'
        (картинка из атрибуции не найдена).
    """
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    here = Path(path).parent
    comments = []
    for match in re.finditer(r'/\*.*?\*/', text, re.S):
        file_m = _ATTR_FILE_RE.search(match.group())
        box_m = _ATTR_BOX_RE.search(match.group())
        box = None
        if box_m:
            vals = tuple(float(v) for v in box_m.groups())
            if all(0 <= v <= 1 for v in vals) and vals[0] < vals[2] and vals[1] < vals[3]:
                box = vals
        comments.append((match.start(), match.end(),
                         file_m.group() if file_m else None, box))
    entries = []
    for match in _LITERAL_RE.finditer(text):
        if any(start < match.start() < end for start, end, _, _ in comments):
            continue  # литерал внутри комментария — цитата, а не заявка
        color = _literal_hex(match)
        if not color:
            continue
        near = min(comments, default=None,
                   key=lambda c: (max(c[0] - match.end(), match.start() - c[1], 0),
                                  abs(c[0] - match.start())))
        dist = max(near[0] - match.end(), match.start() - near[1], 0) if near else gap + 1
        line = text[:match.start()].count('\n') + 1
        rgb = tuple(int(color[1 + i * 2:3 + i * 2], 16) for i in range(3))
        entry = {'color': color, 'luma': _luma(rgb), 'line': line,
                 'file': near[2] if dist <= gap else None,
                 'box': near[3] if dist <= gap else None}
        if dist > gap or not near[2] or not near[3]:
            entry['status'] = 'unattributed'
        else:
            found = next((c for c in (here / near[2], here / 'public' / 'images' / near[2],
                                      here / 'public' / near[2], here / 'images' / near[2])
                          if c.exists()), None)
            if not found:
                entry['status'] = 'missing'
            else:
                entry['file'] = str(found)
                mode = Counter(_window(_load(str(found)), near[3])).most_common(1)[0][0]
                entry['measured'], entry['measured_luma'] = mode, _luma(mode)
                entry['status'] = 'ok' if all(abs(a - b) <= tol
                                              for a, b in zip(rgb, mode)) else 'drifted'
        entries.append(entry)
    summary = Counter(e['status'] for e in entries)
    summary = {s: summary.get(s, 0) for s in ('ok', 'drifted', 'unattributed', 'missing')}
    return {'file': path, 'tol': tol, 'colors': entries, 'summary': summary,
            'ok': not (summary['drifted'] or summary['unattributed'] or summary['missing'])}


def image_frac(path: str, rect: tuple = None, box: tuple = None,
               crop: tuple = None) -> dict:
    """
    Пересчитать координаты хотспотов: пиксели снимка ↔ доли кадра, с вычетом
    отрезаемых полей (статус-бара, панели навигации).

    Арифметика повторяется в каждой сборке и каждый раз считается вручную:
    `(y − 91) / 2186`, «hotspot 66 px — это какая доля?», «окно `(0.05,0.30,
    0.95,0.62)` — это какие пиксели кропнутого кадра?». Инструмент делает перевод
    в обе стороны: `rect` — прямоугольник (x, y, w, h) в пикселях целого
    кадра, `box` — `(x0, y0, x1, y1)` долями cropped-кадра; `crop` — PIL-box
    `(x0, y0, x1, y1)` вырезаемого окна целого кадра, та запись, что держат
    `.crop(...)` в конвейере и CLAUDE.md проекта (`(0, 91, 1080, 2277)`).

    Args:
        path: картинка или снимок экрана (физические пиксели).
        rect: прямоугольник (x, y, w, h) в пикселях целого кадра.
        box: прямоугольник (x0, y0, x1, y1) долями cropped-кадра.
        crop: PIL-box вырезаемого окна целого кадра; пусто — весь кадр.

    Returns:
        {'file', 'frame': {'w','h'} целого кадра, 'crop', 'cropped': {'w','h'},
         для rect: 'px_cropped' и 'frac' {'x0','y0','x1','y1'};
         для box: 'px' (в целого кадра) и 'px_cropped'}.

    Raises:
        ValueError: ни rect, ни box; или crop-box не лежит в кадре.
    """
    img = _load(path)
    w, h = img.size
    crop = tuple(int(v) for v in crop) if crop else (0, 0, w, h)
    cx0, cy0, cx1, cy1 = crop
    if not (0 <= cx0 < cx1 <= w and 0 <= cy0 < cy1 <= h):
        raise ValueError(f'crop {tuple(crop)} на кадре {w}x{h}: окно не лежит '
                         'внутри кадра (ждём PIL-box x0,y0,x1,y1)')
    cw, ch = cx1 - cx0, cy1 - cy0
    result = {'file': path, 'frame': {'w': w, 'h': h},
              'crop': tuple(crop), 'cropped': {'w': cw, 'h': ch}}
    if rect:
        x, y, rw, rh = (int(v) for v in rect)
        x, y = x - cx0, y - cy0
        result['px_cropped'] = {'x': x, 'y': y, 'w': rw, 'h': rh}
        result['frac'] = {'x0': round(x / cw, 4), 'y0': round(y / ch, 4),
                          'x1': round((x + rw) / cw, 4), 'y1': round((y + rh) / ch, 4)}
    elif box:
        x0, y0, x1, y1 = (float(v) for v in box)
        result['px'] = {'x': round(x0 * cw) + cx0, 'y': round(y0 * ch) + cy0,
                        'w': round((x1 - x0) * cw), 'h': round((y1 - y0) * ch)}
        result['px_cropped'] = {'x': round(x0 * cw), 'y': round(y0 * ch),
                                'w': round((x1 - x0) * cw), 'h': round((y1 - y0) * ch)}
    else:
        raise ValueError('нужен rect (пиксели) или box (доли кадра)')
    return result


def _seam_measure(img: Image.Image, pos: int, mid: int, axis: str, span: int,
                  band: int, tol: int = IMAGE_SEAM_STEP) -> dict:
    """Профиль через границу и вердикт для проверенного места полосы."""
    w, h = img.size
    along, across = (w, h) if axis == 'x' else (h, w)
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
    return {'at': mid, 'a': _median_luma_profile(profile[:k]),
            'b': _median_luma_profile(profile[k:]),
            'delta': _median_luma_profile(profile[:k]) - _median_luma_profile(profile[k:]),
            'max_step': max_step, 'profile_max': max(steps, default=0),
            'profile': profile, 'edge': max_step >= tol}


def _seam_scan(img: Image.Image, pos: int, axis: str, span: int, band: int,
               lo: int = None, hi: int = None, tol: int = IMAGE_SEAM_STEP) -> dict:
    """
    Найти спокойное место полосы и решить по нему.

    Положения перебираются поперёк оси в пределах [lo, hi) (по умолчанию —
    весь кадр), шагом не меньше толщины полосы; спокойное место выбирается
    по ступени в окне вердикта, при равенстве — ближе к центру. Доля проб
    с тем же вердиктом — про стойкость: ступенька текста уходит, настоящая — нет.
    """
    w, h = img.size
    across = h if axis == 'x' else w
    lo, hi = (band // 2, across - band // 2) if lo is None else (lo, hi)
    step = max(band, (hi - lo) // IMAGE_SEAM_SCAN or band)
    probes = [_seam_measure(img, pos, mid, axis, span, band, tol=tol)
              for mid in range(lo, max(lo + 1, hi), step)]
    center = (lo + hi) // 2
    best = min(probes, key=lambda p: (p['max_step'], abs(p['at'] - center)))
    best['agree'] = sum(1 for p in probes if p['edge'] == best['edge'])
    best['scanned'] = len(probes)
    return best


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


def _literal_hex(match):
    """
    Hex-краска '#rrggbb' из совпадения `_LITERAL_RE`; None — если это не цвет
    ('#12345' — ни 3, ни 6 знаков). 4- и 8-значные теряют альфу: контракт
    тону держится по каналу, альфа в рукописных страницах не встречается.
    """
    if match.group(1):
        body = match.group(1).lower()
        if len(body) in (3, 4):
            body = ''.join(c * 2 for c in body[:3])
        elif len(body) in (6, 8):
            body = body[:6]
        else:
            return None
    else:
        body = '%02x%02x%02x' % tuple(int(g) for g in match.groups()[1:])
    return '#' + body


def _stroke_trace(img: Image.Image, pos: int, mid: int, axis: str,
                  pad: int, contrast: int):
    """
    Сечение линии в одной точке вдоль стороны: (центр, ширина ядра, цвет ядра)
    или None. Яркость каждого столбца — медиана поперёк полосы, чтобы текст и
    шум одного-двух пикселей не сдвигали центр; ядро — подряд идущие столбцы
    с отклонением от местного фона не меньше `contrast` вокруг точки самого
    сильного отклонения. Линия ищется и тёмной (рамка на карточке), и светлой
    (рамка на тёмном фоне) — поэтому отклонение, а не «темнее фона».
    """
    px = img.load()
    w, h = img.size
    along, across = (w, h) if axis == 'x' else (h, w)
    r_lo = max(0, mid - IMAGE_STROKE_BAND // 2)
    r_hi = min(across, mid + IMAGE_STROKE_BAND // 2 + 1)
    c_lo, c_hi = max(0, pos - pad), min(along, pos + pad + 1)
    cols = []
    for c in range(c_lo, c_hi):
        pix = ([px[c, r] for r in range(r_lo, r_hi)] if axis == 'x'
               else [px[r, c] for r in range(r_lo, r_hi)])
        cols.append((_median_luma_profile([_luma(p) for p in pix]), pix))
    if not cols:
        return None
    bg = _median_luma_profile([lum for lum, _ in cols])
    d = max(range(len(cols)), key=lambda i: abs(cols[i][0] - bg))
    if abs(cols[d][0] - bg) < contrast:
        return None
    left = right = d
    while left > 0 and abs(cols[left - 1][0] - bg) >= contrast:
        left -= 1
    while right < len(cols) - 1 and abs(cols[right + 1][0] - bg) >= contrast:
        right += 1
    core = [p for _, pix in cols[left:right + 1] for p in pix]
    rgb = tuple(_median_luma_profile(list(ch)) for ch in zip(*core))
    return c_lo + (left + right) / 2.0, right - left + 1, rgb


def _stroke_side(pos: int, trace: list):
    """
    Вердикт стороны по трассе: {'pos','offset','width','rgb','luma'} и сама
    трасса. Положение — медиана центров: дуги углов занимают меньше половины
    стороны и медиану не сдвигают; ширина и цвет считаются по прямому участку
    (центры в шаге от медианы), иначе места слияния с соседней стороной
    раздули бы ядро.
    """
    if not trace:
        return {'none': 'линии с заданным контрастом нет в пределах pad'}, []
    got = round(_median_luma_profile([t[1] for t in trace]))
    core = [t for t in trace if abs(t[1] - got) <= 1.0] or trace
    width = Counter(t[2] for t in core).most_common(1)[0][0]
    rgb = tuple(_median_luma_profile(list(ch)) for ch in zip(*(t[3] for t in core)))
    return {'pos': got, 'offset': got - pos, 'width': width, 'rgb': rgb,
            'luma': _luma(rgb)}, trace


def _corner_radius(pts: list, pad: int) -> float:
    """
    Радиус дуги по точкам (расстояние вдоль стороны от угла, inward-уход
    центра): у скруглённого угла центр дуги живёт в (r, r) от угла, вот r —
    единственная степень свободы; перебором минимизируем невязку расстояний
    точек до окружности. Дуга из точек в паре пикселей плоская, но якоря
    жёсткие: ошибись центр — и все точки сойдутся только на истинном r.
    """
    best_r, best_err = 0.0, None
    for step in range(int(pad * 4) + 1):
        r = step / 4
        err = sum((((x - r) ** 2 + (y - r) ** 2) ** 0.5 - r) ** 2 for x, y in pts)
        if best_err is None or err < best_err:
            best_r, best_err = r, err
    return best_r


def _interp(xs: list, ys: list, v: int) -> int:
    """Кусочно-линейная интерполяция по якорям xs (строго растущие) → ys."""
    if v <= xs[0]:
        return ys[0]
    for hi in range(1, len(xs)):
        if v <= xs[hi]:
            lo = hi - 1
            return round(ys[lo] + (ys[hi] - ys[lo]) * (v - xs[lo]) / (xs[hi] - xs[lo]))
    return ys[-1]


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Замер яркости фона, тоновая подгонка по эталону, аудит окон, '
                    'проверка швов, сравнение кадров, инвентарь цветов разметки, '
                    'измерение обводки, перенос тона между кадрами, тоновый '
                    'контракт разметки, координаты хотспотов.')
    parser.add_argument('command',
                        choices=['measure', 'match', 'audit', 'seam',
                                 'diff', 'rect-seams', 'literals',
                                 'stroke', 'expose', 'contract', 'frac'])
    parser.add_argument('files', nargs='+', help='картинки (для seam/rect-seams — одна, для diff — две)')
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
    parser.add_argument('--best', action='store_true',
                        help='seam: перебрать полосу и решить по спокойному месту (не верить одному --at)')
    parser.add_argument('--rect', default='',
                        help='rect-seams/stroke/frac: прямоугольник x,y,w,h (запись screen из adb_cdp element-rect)')
    parser.add_argument('--pad', type=int, default=IMAGE_STROKE_PAD,
                        help='stroke: сколько пикселей вокруг стороны искать линию (и потолок радиуса)')
    parser.add_argument('--contrast', type=int, default=IMAGE_STROKE_CONTRAST,
                        help='stroke: отклонение яркости от местного фона, принадлежащее штриху')
    parser.add_argument('--pair', action='append', default=[],
                        help='expose: окно-референс x0,y0,x1,y1 долями кадра (повторяемо, один и тот же в обоих кадрах)')
    parser.add_argument('--color', action='append', default=[],
                        help='expose: цвет r,g,b для переноса (повторяемо)')
    parser.add_argument('--crop', default='',
                        help='frac: PIL-box вырезаемого окна x0,y0,x1,y1 целого кадра, как `.crop((0, 91, 1080, 2277))` в конвейере')
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
                s = image_seam(f, ns.pos, at=ns.at, axis=ns.axis, span=ns.span,
                               band=ns.band, best=ns.best)
                verdict = (f'край виден: ступень {s["max_step"]}' if s['edge']
                           else 'стыка не видно')
                if ns.best:
                    verdict += f' (согласно {s["agree"]}/{s["scanned"]} проб)'
                print(f'{f}: {ns.axis}={ns.pos} at={s["at"]} — до {s["a"]}, после {s["b"]} '
                      f'(Δ {s["delta"]}, max_step {s["max_step"]}) — {verdict}')
        elif ns.command == 'diff':
            if len(ns.files) != 2:
                raise SystemExit('нужно ровно два кадра: эталон и проверяемый')
            d = image_diff_zones(ns.files[0], ns.files[1])
            print(f'{ns.files[0]} vs {ns.files[1]}:')
            for i, z in enumerate(d['zones']):
                print(f'  зона {i + 1} {z["box"]}: L {z["a"]:>3} vs {z["b"]:>3} '
                      f'(Δ {z["delta"]:>3}) {"≠" if z["edge"] else "="}')
            for w in d['warnings']:
                print(f'  ВНИМАНИЕ: {w}')
        elif ns.command == 'rect-seams':
            if not ns.rect:
                raise SystemExit('нужен --rect: x,y,w,h (запись screen из adb_cdp element-rect)')
            rect = tuple(int(v) for v in ns.rect.split(','))
            if len(rect) != 4:
                raise SystemExit('--rect: ждём x,y,w,h')
            for f in ns.files:
                r = image_rect_seams(f, rect)
                print(f'{f}: rect {r["rect"]} — ' + ('швов нет' if r['ok'] else 'ЕСТЬ ШОВ'))
                for name, s in r['sides'].items():
                    if 'skip' in s:
                        print(f'  {name}: {s["skip"]}')
                    else:
                        verdict = (f'край виден: ступень {s["max_step"]}' if s['edge']
                                   else 'стыка не видно')
                        print(f'  {name}: at={s["at"]} — до {s["a"]}, после {s["b"]} '
                              f'(max_step {s["max_step"]}, согласно {s["agree"]}/{s["scanned"]}) — {verdict}')
        elif ns.command == 'literals':
            for f in ns.files:
                for c in image_literals(f)['colors']:
                    print(f'{f}: {c["color"]} L={c["luma"]:>3} ×{c["count"]}')
        elif ns.command == 'stroke':
            rect = tuple(int(v) for v in ns.rect.split(',')) if ns.rect else None
            if rect is not None and len(rect) != 4:
                raise SystemExit('--rect: ждём x,y,w,h')
            for f in ns.files:
                s = image_stroke(f, rect=rect, pad=ns.pad, contrast=ns.contrast)
                head = 'рамка есть' if s['found'] else 'рамки нет'
                print(f'{f}: rect {s["rect"]} — {head}'
                      + (f', радиус {s["radius"]}' if s['radius'] is not None else ''))
                for name, side in s['sides'].items():
                    if 'none' in side:
                        print(f'  {name}: {side["none"]}')
                    else:
                        print(f'  {name}: pos={side["pos"]} (сдвиг {side["offset"]:+d}), '
                              f'ширина {side["width"]} px, ядро {side["rgb"]} L={side["luma"]}')
                if s['css']:
                    print(f'  в CSS: border {s["css"]["border_vw"]}vw'
                          + (f', radius {s["css"]["radius_vw"]}vw'
                             if s['css']['radius_vw'] is not None else ''))
        elif ns.command == 'expose':
            if len(ns.files) != 2:
                raise SystemExit('нужно ровно два кадра: источник и цель')
            if not ns.pair:
                raise SystemExit('нужен хотя бы один --pair: окно-референс x0,y0,x1,y1')
            pairs = [tuple(float(v) for v in p.split(',')) for p in ns.pair]
            colors = [tuple(int(v) for v in c.split(',')) for c in ns.color] or None
            r = image_expose(ns.files[0], ns.files[1], pairs, colors=colors)
            print(f'{ns.files[0]} → {ns.files[1]}:')
            for ref in r['pairs']:
                print(f'  окно {tuple(round(v, 3) for v in ref["box"])}: '
                      f'{ref["a"]} L={ref["a_luma"]} → {ref["b"]} L={ref["b_luma"]}')
            print(f'  усиление {r["gains"]}, нелинейность {r["residual"]}')
            for m in r['mapped']:
                print(f'  {m["from"]} → {m["to"]}')
        elif ns.command == 'contract':
            for f in ns.files:
                c = image_contract(f)
                print(f'{f}: ' + ', '.join(f'{k} {v}' for k, v in c['summary'].items())
                      + (' — контракт цел' if c['ok'] else ' — НАРУШЕН'))
                for e in c['colors']:
                    if e['status'] == 'ok':
                        continue
                    detail = f' ({e["file"]}' if e.get('file') else ''
                    if e.get('measured'):
                        detail += f', измерено {e["measured"]} L={e["measured_luma"]}'
                    detail += ')' if e.get('file') else ''
                    print(f'  строка {e["line"]}: {e["color"]} L={e["luma"]:>3} — '
                          f'{e["status"]}{detail}')
        elif ns.command == 'frac':
            crop = tuple(int(v) for v in ns.crop.split(',')) if ns.crop else None
            rect = tuple(int(v) for v in ns.rect.split(',')) if ns.rect else None
            for f in ns.files:
                r = image_frac(f, rect=rect, box=box, crop=crop)
                print(f'{f}: кадр {r["frame"]} −{r["crop"]} → {r["cropped"]}')
                if 'frac' in r:
                    print(f'  rect {r["px_cropped"]} → доли {r["frac"]}')
                else:
                    print(f'  box {box} → px {r["px"]} (в crop-кадре {r["px_cropped"]})')
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

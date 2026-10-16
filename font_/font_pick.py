"""Шрифт, которым можно написать вот этот текст: найти установленный в системе.

Подписать картинку нечем: Pillow рисует только тем шрифтом, который ему дали,
а встроенный (`ImageFont.load_default`) знает латиницу и больше ничего. Русская
подпись выходит рядом пустых прямоугольников — и заметно это не в коде, а на
готовом кадре, когда он уже уехал человеку.

```python
from font_.font_pick import font_pick, font_covers

font = font_pick(28, 'Шаг 7')
draw.text((x, y), 'Шаг 7' if font_covers(font, 'Шаг 7') else 'Step 7', font=font)
```

## ⚠⚠ Покрытие проверяется РАСТРОМ, а не таблицей cmap

`font_coverage` из соседнего файла отвечает на тот же вопрос точнее, но ему
нужен путь к файлу и `fontTools` — а встроенный шрифт Pillow файла не имеет
вовсе. Здесь сравнивается отрисовка: символ, которого в шрифте нет, рисуется
тем же «.notdef», что и заведомо отсутствующий знак из приватной области.
Приём работает для любого шрифта, включая встроенный, и не тянет зависимостей
сверх Pillow.

## ⚠ Список путей, а не `fc-list`

`fontconfig` есть не везде: в `python:*-slim`, на котором стоит половина
контейнеров, нет ни его, ни шрифтов вообще. Вызов внешней команды дал бы
отказ там, где нужен ответ «ничего нет», — и стоил бы процесса на каждую
подпись.
"""

import pathlib

# Где смотреть шрифты. ⚠ Порядок — порядок предпочтения: DejaVu первым, у него
# самое широкое покрытие из тех, что ставят пакетами (кириллица, греческий,
# знаки). Дальше — то, что встречается в образах и на машинах разработчиков.
FONT_PICK_PATHS = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
    '/usr/share/fonts/TTF/DejaVuSans.ttf',
    '/Library/Fonts/Arial Unicode.ttf',
    '/System/Library/Fonts/Supplemental/Arial.ttf',
    'C:/Windows/Fonts/arial.ttf',
)

# Каталоги, которые обходим, если ни один известный путь не подошёл. ⚠ Обход
# второй очередью, а не первой: он стоит сотен обращений к диску, а известные
# пути закрывают девять случаев из десяти.
FONT_PICK_DIRS = (
    '/usr/share/fonts',
    '/usr/local/share/fonts',
)

# Сколько файлов смотрим при обходе. ⚠ Предел обязателен: на машине с
# установленным офисным пакетом шрифтов тысячи, и перебор всех ради подписи в
# три слова — это секунды на каждый кадр.
FONT_PICK_SCAN_MAX = 60

# Знак, которого в шрифтах не бывает: приватная область Unicode. Его растр —
# образец «.notdef», с которым сравниваются проверяемые символы.
FONT_PICK_ABSENT = '\ue000'


def font_pick(size: int, text: str = ''):
    """Шрифт нужного кегля, по возможности умеющий написать `text`.

    Args:
        size: кегль в точках.
        text: что собираемся писать; пусто — сойдёт любой шрифт.

    Returns:
        Объект шрифта Pillow. **Всегда** — не нашлось подходящего, вернётся
        встроенный: подпись латиницей лучше отказа рисовать кадр.

    ⚠⚠ Проверяйте результат `font_covers`, если текст не латинский. Встроенный
    шрифт кириллицу не знает и нарисует пустые прямоугольники — молча, и
    увидите вы это на готовой картинке.

    Raises:
        RuntimeError: нет Pillow.
    """
    font_mod = _pillow_font()

    for path in FONT_PICK_PATHS:
        font = _font_load(path, size)
        if font and (not text or font_covers(font, text)):
            return font

    for path in _font_scan():
        font = _font_load(path, size)
        if font and (not text or font_covers(font, text)):
            return font

    # ⚠ `size` у встроенного поддерживается с Pillow 10.1; на старой версии
    # вернётся крошечный растровый шрифт, и это лучше отказа.
    try:
        return font_mod.load_default(size=size)
    except TypeError:
        return font_mod.load_default()


def font_covers(font, text: str) -> bool:
    """Умеет ли шрифт написать этот текст; `False` — будут прямоугольники.

    ⚠ Сравнение растров, а не таблицы символов: см. шапку модуля. Пробелы и
    переносы пропускаются — у них нет видимого глифа, и «.notdef» им не
    грозит.

    ⚠ Не бросает на странном шрифте: отвечает `False`. Вопрос задают, чтобы
    выбрать подпись, а не чтобы получить отказ посреди рисования.
    """
    if not text:
        return True

    # ⚠ `bytes(...)`, а не `.tobytes()`: `getmask` отдаёт `ImagingCore`, и у
    # него такого метода нет — зато есть буферный протокол. Ошибка эта тихая,
    # её ловит `except` ниже и отвечает «не покрывает» на любом шрифте.
    try:
        absent = bytes(font.getmask(FONT_PICK_ABSENT))
        for one in set(text):
            if one.isspace():
                continue
            if bytes(font.getmask(one)) == absent:
                return False
    except Exception:  # noqa: BLE001 — подпись не стоит прогона
        return False

    return True


# ── детали реализации ──────────────────────────────────────────────────────

def _pillow_font():
    """Модуль `ImageFont` Pillow; нет его — отказ словами, а не `ImportError`."""
    try:
        from PIL import ImageFont
    except ImportError as fault:
        raise RuntimeError('для подписи нужен Pillow: pip install Pillow') \
            from fault

    return ImageFont


def _font_load(path: str, size: int):
    """Шрифт по пути; файла нет или он не шрифт — `None`, без отказа."""
    try:
        return _pillow_font().truetype(path, size)
    except Exception:  # noqa: BLE001 — перебираем кандидатов, а не падаем
        return None


def _font_scan() -> list:
    """Шрифты из системных каталогов, не больше `FONT_PICK_SCAN_MAX`.

    ⚠ Сортировка по имени нужна не для красоты, а ради повторяемости: без неё
    порядок обхода зависит от файловой системы, и подпись меняла бы начертание
    от машины к машине.
    """
    out = []
    for where in FONT_PICK_DIRS:
        root = pathlib.Path(where)
        if not root.is_dir():
            continue
        for one in sorted(root.rglob('*.tt[fc]')):
            out.append(str(one))
            if len(out) >= FONT_PICK_SCAN_MAX:
                return out

    return out

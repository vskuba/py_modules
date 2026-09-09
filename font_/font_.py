"""
Шрифты: паспорт, покрытие символов, сверка мастера и подмножества,
подбор кегля по чернилам.

Модуль заточен под главный вопрос форензики шрифта — «это тот же шрифт?».
Прямое сравнение таблиц лжёт: движки печати перекодируют CFF в glyf и
меняют unitsPerEm, поэтому контуры сравниваются нормированными по upem
(`font_compare`), а когда форматы разные — растром одинакового текста по
базовой линии (`font_text_diff`). Метрическая сторона сборки: чернильная
рамка строки (`font_text_ink`) и наибольший влезающий кегль
(`font_fit_size`). Подробности и грабли — `font_tooling.md`.
"""
import re

SUBSET_RE = re.compile(r"^[A-Z]{6}\+")


def font_info(path: str | bytes) -> dict:
    """
    Паспорт шрифта: формат, unitsPerEm, число глифов, hhea-выступы, имена,
    признак подмножества (префикс `ABCDEF+` в PostScript-имени), число
    покрытых cmap-символов.

    Принимает путь, байты или бинарный поток: подмножество, извлечённое из PDF
    (`pdf_fonts_extract`), приезжает байтами, и ради одного вызова его никто
    не обязан класть на диск. Поток читается до конца — для повторного вызова
    нужен новый поток или байты.

    Метрики (`ascender`/`descender`) — в единицах upem: сравнивать их между
    собой можно только разделив на upem, у перекодированного подмножества
    они в разы больше при том же рисунке глифов.
    """
    from fontTools.ttLib import TTFont

    t = TTFont(_stream(path))
    name = t["name"]

    def _n(name_id: int) -> str:
        # getDebugName — единственный честный способ: у таблицы name нет .get,
        # и она многоязычная (проверено живьём на сюжете).
        return name.getDebugName(name_id) or ""

    postscript = _n(6)
    return {
        "format": _format(t),
        "upem": t["head"].unitsPerEm,
        "num_glyphs": t["maxp"].numGlyphs,
        "ascender": t["hhea"].ascent,
        "descender": t["hhea"].descent,
        "family": _n(1),
        "subfamily": _n(2),
        "postscript": postscript,
        "subset_prefix": bool(SUBSET_RE.match(postscript)),
        "chars": len(t.getBestCmap() or {}),
    }


def font_baseline_top(path: str | bytes, size_pt: float) -> float:
    """
    Смещение от верха строки `line-height: 1em` до базовой линии, пункты.

    Chrome/WebView ставит текст не по верху глифов: содержимое строки
    (ascender − descender) отличается от 1em, разница делится полуинтервалами
    (half-leading), и baseline уходит на `asc + (1 − asc + desc)/2` =
    `(1 + asc + desc)/2` em от верха строки (descender со знаком, обычно
    отрицательный). Это единственный способ руками посадить HTML-текст в
    baseline, который Skia заняла в исходном документе: офсет в пикселях
    шаблона — ответ этой функции.

    Флаг USE_TYPO_METRICS (бит 0x80 в OS/2.fsSelection) переключает движок на
    sTypoAscender/sTypoDescender: у официальных шрифтов hhea-выступы нарочно
    раздуты, и без учёта флага текст сядет ниже фактической строки.
    """
    from fontTools.ttLib import TTFont

    t = TTFont(_stream(path))
    upem = t["head"].unitsPerEm
    os2 = t.get("OS/2")
    if os2 is not None and (os2.fsSelection & 0x80):
        asc, desc = os2.sTypoAscender, os2.sTypoDescender
    else:
        asc, desc = t["hhea"].ascent, t["hhea"].descent
    return size_pt * (1 + asc / upem + desc / upem) / 2


def font_coverage(path: str | bytes, text: str) -> list[str]:
    """
    Символы `text`, отсутствующих в таблице cmap, в порядке появления.

    Проверка для многоязычного текста: отсутствие символа в cmap — это не
    «нарисуется кривой», а дырка/фоллбэк чужим шрифтом; у субсета, собранного
    под один абзац, дырок может быть не видно на глаз.
    """
    from fontTools.ttLib import TTFont

    cmap = TTFont(_stream(path)).getBestCmap() or {}
    missing = []
    for ch in text:
        if ord(ch) not in cmap and ch not in missing:
            missing.append(ch)
    return missing


def font_compare(path_a: str | bytes, path_b: str | bytes) -> dict:
    """
    Сверка двух шрифтов: набор глифов (без `.notdef`) и совпадение контуров
    общих глифов, нормированных по unitsPerEm каждого.

    Нормировка — суть функции: подмножество из PDF часто несёт те же контуры,
    отмасштабированные вместе со сменой upem (512 -> 2048, координаты x4);
    без деления на upem сравнение показало бы различие всего.

    Форматы (`formats`) разных библиотек рисуют кривые по-разному
    (glyf — квадратичные, CFF — кубические): при разных форматах поле
    `outlines_differ` не читается, спор решается `font_text_diff` по растру.
    """
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTFont

    a, b = TTFont(_stream(path_a)), TTFont(_stream(path_b))
    upem_a, upem_b = a["head"].unitsPerEm, b["head"].unitsPerEm
    names_a = set(a.getGlyphOrder()) - {".notdef"}
    names_b = set(b.getGlyphOrder()) - {".notdef"}
    common = sorted(names_a & names_b)

    differ = []
    for name in common:
        pa, pb = RecordingPen(), RecordingPen()
        a.getGlyphSet()[name].draw(pa)
        b.getGlyphSet()[name].draw(pb)
        if _norm(pa.value, upem_a) != _norm(pb.value, upem_b):
            differ.append(name)

    return {
        "count_a": len(names_a), "count_b": len(names_b),
        "only_a": sorted(names_a - names_b), "only_b": sorted(names_b - names_a),
        "common": len(common), "outlines_differ": differ,
        "upems": [upem_a, upem_b], "formats": [_format(a), _format(b)],
    }


def font_render_text(path: str | bytes, text: str, out_png: str, size: int = 64) -> str:
    """
    Растр строки `text` шрифтом `path` (файл, байты или поток): чёрное на
    белом, базовая линия на 2/3 высоты холста (`anchor="ls"`).

    Базовая линия — не деталька: у разных шрифтов собственные выступы, и
    без выравнивания по ней разница метрик выглядит как разница глифов на
    всей картинке.
    """
    import os

    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(os.path.dirname(os.path.abspath(out_png)) or ".", exist_ok=True)
    font = ImageFont.truetype(_stream(path), size)
    canvas = Image.new("L", (max(size * 2, size * len(text) * 2), size * 3), 255)
    ImageDraw.Draw(canvas).text((size // 2, size * 2), text, font=font, fill=0, anchor="ls")
    canvas.convert("RGB").save(out_png)
    return out_png


def font_text_ink(path: str | bytes, text: str, size: int = 64) -> dict:
    """
    Чернильная рамка строки в заданном кегле, относительно базовой линии.

    Метрики шрифта (hhea/OS2) описывают em-ящик, а не то, что реально
    отрисовалось: капитель сверяют с телефоном по чернилам, отступы
    подписей — тоже. Поэтому растр: строка рисуется на чёрном холсте с
    запасом (выносные глифы не должны обрезать краем), берётся габарит
    непустых пикселей — антиалиасинг считается чернилом, как он считается
    на снимке. `font.getbbox` для этого не годится: он отдаёт ящик по
    метрике advance, а не чернила (проверено на синтетике: 0..advance
    вместо 0.1..0.9 контура).

    y отрицателен над базовой линией (ascent = -y0), x — от левого края
    строки. Пустая строка и строка без чернил (пробелы) — ValueError.
    """
    from PIL import Image, ImageDraw, ImageFont

    if not text:
        raise ValueError('порожня рядка — вимірювати чернила нічого')
    font = ImageFont.truetype(_stream(path), size)
    ox, oy = size, size * 2          # левый край строки, базовая линия
    canvas = Image.new("L", (size * (len(text) + 2), size * 4), 0)
    ImageDraw.Draw(canvas).text((ox, oy), text, font=font, fill=255, anchor="ls")
    bbox = canvas.getbbox()
    if bbox is None:
        raise ValueError(f'текст {text!r} не лишив чорнила (самі пробіли?)')
    x0, y0, x1, y1 = bbox
    return {"x0": x0 - ox, "y0": y0 - oy, "x1": x1 - ox, "y1": y1 - oy,
            "w": x1 - x0, "h": y1 - y0, "ascent": oy - y0, "size": size}


def font_fit_size(path: str | bytes, text: str, target_w: int,
                  min_size: int = 1, max_size: int = 2000) -> int:
    """
    Наибольший кегль, при котором чернила строки шире `target_w` пикселя не станут.

    Заголовок вмеряют в колонку с телефона: ширина цели известна из
    `image_scan`-окнаoriginalа, кегль подбирают под неё. Двоичный поиск по
    чернильной ширине (`font_text_ink`) — она неубывающая по кеглю;
    растровая, то есть с учётом хантинга, ±1 px по краям — норма.
    """
    def ink_w(size):
        return font_text_ink(path, text, size)["w"]

    if ink_w(min_size) > target_w:
        raise ValueError(f'навіть кегль {min_size} лишає {ink_w(min_size)} px — '
                         f'ціль {target_w} px недосяжна')
    lo, hi = min_size, max_size
    if ink_w(hi) <= target_w:
        return hi
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ink_w(mid) <= target_w:
            lo = mid
        else:
            hi = mid
    return lo


def font_text_diff(path_a: str | bytes, path_b: str | bytes, text: str, size: int = 64,
                   out_dir: str | None = None) -> dict:
    """
    Спор «это тот же рисунок?» растром: одна и та же строка, отрисованная
    обоими шрифтами по общей базовой линии, и попиксельная статистика.

    Применяется, когда контуры напрямую несравнимы (форматы разные, либо
    один из файлов — подмножество из PDF). Порог решения: доли процента —
    округление растеризатора; проценты и больше — формы разошлись.
    `out_dir` — положить `a.png`, `b.png`, `diff.png` для глазной проверки.
    """
    import os
    import tempfile

    from PIL import Image, ImageChops

    with tempfile.TemporaryDirectory() as d:
        pa = font_render_text(path_a, text, f"{d}/a.png", size=size)
        pb = font_render_text(path_b, text, f"{d}/b.png", size=size)
        ia, ib = Image.open(pa), Image.open(pb)
        if ia.size != ib.size:
            ib = ib.resize(ia.size)
        result = _image_stats(ia, ib)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            heat = ImageChops.difference(ia.convert("RGB"), ib.convert("RGB")).convert("L").convert("RGB")
            for src, name in ((pa, "a.png"), (pb, "b.png")):
                Image.open(src).save(f"{out_dir}/{name}")
            heat.save(f"{out_dir}/diff.png")
            result["images"] = {"a": f"{out_dir}/a.png", "b": f"{out_dir}/b.png", "diff": f"{out_dir}/diff.png"}
        else:
            result["images"] = None
    return result


def main() -> None:
    """CLI: `python -m font_.font_ info|coverage|compare|render|ink|fit|textdiff|baseline-top …`."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m font_.font_")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info"); p.add_argument("font")
    p = sub.add_parser("coverage"); p.add_argument("font"); p.add_argument("text")
    p = sub.add_parser("compare"); p.add_argument("a"); p.add_argument("b")
    p = sub.add_parser("render"); p.add_argument("font"); p.add_argument("text"); p.add_argument("out_png"); p.add_argument("--size", type=int, default=64)
    p = sub.add_parser("ink"); p.add_argument("font"); p.add_argument("text"); p.add_argument("--size", type=int, default=64)
    p = sub.add_parser("fit"); p.add_argument("font"); p.add_argument("text"); p.add_argument("--target-w", type=int, required=True); p.add_argument("--max-size", type=int, default=2000)
    p = sub.add_parser("textdiff"); p.add_argument("a"); p.add_argument("b"); p.add_argument("text"); p.add_argument("--size", type=int, default=64); p.add_argument("--out-dir")
    p = sub.add_parser("baseline-top"); p.add_argument("font"); p.add_argument("size_pt", type=float)

    a = parser.parse_args()
    if a.cmd == "info":
        print(json.dumps(font_info(a.font), ensure_ascii=False, indent=1))
    elif a.cmd == "coverage":
        print("\n".join(font_coverage(a.font, a.text)) or "— покрыто всё")
    elif a.cmd == "compare":
        print(json.dumps(font_compare(a.a, a.b), ensure_ascii=False, indent=1))
    elif a.cmd == "render":
        print(font_render_text(a.font, a.text, a.out_png, size=a.size))
    elif a.cmd == "ink":
        print(json.dumps(font_text_ink(a.font, a.text, size=a.size), ensure_ascii=False))
    elif a.cmd == "fit":
        # Целое число, не JSON: значение подставляют в CSS/шаблон.
        print(font_fit_size(a.font, a.text, a.target_w, max_size=a.max_size))
    elif a.cmd == "textdiff":
        print(json.dumps(font_text_diff(a.a, a.b, a.text, size=a.size, out_dir=a.out_dir), ensure_ascii=False, indent=1))
    elif a.cmd == "baseline-top":
        # Число с плавающей точкой, не JSON: значение подставляют в CSS/шаблон.
        print(f"{font_baseline_top(a.font, a.size_pt):.3f}")



def _stream(source):
    """Байты обёртывает в поток: PIL сырых байтов не переваривает, fontTools —
    да, а вход у функций общий (путь / байты / поток)."""
    import io

    return io.BytesIO(source) if isinstance(source, bytes) else source


def _format(tt) -> str:
    return "CFF" if "CFF " in tt else "TrueType"


def _norm(value, upem: int):
    """Нормировка записей пера к единицам upem: координаты /upem, остальное как есть."""
    def walk(obj):
        if isinstance(obj, tuple):
            return tuple(walk(v) for v in obj)
        if isinstance(obj, (int, float)):
            return round(obj / upem, 4)
        return obj
    return [(op, walk(args)) for op, args in value]


def _image_stats(img_a, img_b) -> dict:
    """Статистика растровой разницы: максимум по каналам, среднее по L, доля пикселей выше порога.

    Копия статистики из `pdf_` осознанная: модули не должны знать друг о
    друге, а общий файл ради двух вызовов — связность больше экономии.
    """
    from PIL import ImageChops

    diff = ImageChops.difference(img_a.convert("RGB"), img_b.convert("RGB"))
    max_diff = max(diff.getchannel(c).getextrema()[1] for c in ("R", "G", "B"))
    gray = diff.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    mean_abs = sum(v * n for v, n in enumerate(hist)) / total
    threshold = 24
    pct = 100.0 * sum(hist[threshold + 1:]) / total
    return {"max_diff": max_diff, "mean_abs": round(mean_abs, 3),
            "pct_pixels": {"threshold": threshold, "pct": round(pct, 3)}}

if __name__ == "__main__":
    main()

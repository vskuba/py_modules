"""
Шрифты: паспорт, покрытие символов, сверка мастера и подмножества.

Модуль заточен под главный вопрос форензики шрифта — «это тот же шрифт?».
Прямое сравнение таблиц лжёт: движки печати перекодируют CFF в glyf и
меняют unitsPerEm, поэтому контуры сравниваются нормированными по upem
(`font_compare`), а когда форматы разные — растром одинакового текста по
базовой линии (`font_text_diff`). Подробности и грабли — `font_tooling.md`.
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
    """CLI: `python -m font_.font_ info|coverage|compare|render|textdiff …`."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="python -m font_.font_")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info"); p.add_argument("font")
    p = sub.add_parser("coverage"); p.add_argument("font"); p.add_argument("text")
    p = sub.add_parser("compare"); p.add_argument("a"); p.add_argument("b")
    p = sub.add_parser("render"); p.add_argument("font"); p.add_argument("text"); p.add_argument("out_png"); p.add_argument("--size", type=int, default=64)
    p = sub.add_parser("textdiff"); p.add_argument("a"); p.add_argument("b"); p.add_argument("text"); p.add_argument("--size", type=int, default=64); p.add_argument("--out-dir")

    a = parser.parse_args()
    if a.cmd == "info":
        print(json.dumps(font_info(a.font), ensure_ascii=False, indent=1))
    elif a.cmd == "coverage":
        print("\n".join(font_coverage(a.font, a.text)) or "— покрыто всё")
    elif a.cmd == "compare":
        print(json.dumps(font_compare(a.a, a.b), ensure_ascii=False, indent=1))
    elif a.cmd == "render":
        print(font_render_text(a.font, a.text, a.out_png, size=a.size))
    elif a.cmd == "textdiff":
        print(json.dumps(font_text_diff(a.a, a.b, a.text, size=a.size, out_dir=a.out_dir), ensure_ascii=False, indent=1))



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

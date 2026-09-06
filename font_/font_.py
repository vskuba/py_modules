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


def font_info(path: str) -> dict:
    """
    Паспорт шрифта: формат, unitsPerEm, число глифов, hhea-выступы, имена,
    признак подмножества (префикс `ABCDEF+` в PostScript-имени), число
    покрытых cmap-символов.

    Метрики (`ascender`/`descender`) — в единицах upem: сравнивать их между
    собой можно только разделив на upem, у перекодированного подмножества
    они в разы больше при том же рисунке глифов.
    """
    from fontTools.ttLib import TTFont

    t = TTFont(path)
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


def font_coverage(path: str, text: str) -> list[str]:
    """
    Символы `text`, отсутствующих в таблице cmap, в порядке появления.

    Проверка для многоязычного текста: отсутствие символа в cmap — это не
    «нарисуется кривой», а дырка/фоллбэк чужим шрифтом; у субсета, собранного
    под один абзац, дырок может быть не видно на глаз.
    """
    from fontTools.ttLib import TTFont

    cmap = TTFont(path).getBestCmap() or {}
    missing = []
    for ch in text:
        if ord(ch) not in cmap and ch not in missing:
            missing.append(ch)
    return missing


def font_compare(path_a: str, path_b: str) -> dict:
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

    a, b = TTFont(path_a), TTFont(path_b)
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

"""
Span'ы текста PDF: слова с координатами, без межбуквенного мусора.

`pypdf.extract_text` вставляет пробел между всеми буквами — «СКУБА» в
выволоке не ищется. Здесь посимвольные боксы `pypdfium2` склеиваются
обратно в слова: новый span на смену шрифта, кегля или базовой линии, на
разрыв больше 0.3·кегль и на настоящий символ пробела — так пробел
становится границей, а не шумом внутри строки.

Координаты `pypdfium2` — родные PDF-овые, начало внизу слева, а матрица
текстового объекта несёт и базовую линию (`f`), и масштаб (`d`): кегль из
`get_font_size()` бывает до масштаба (35 при печати 17.5), поэтому живой
размер считается произведением. Наружу отдаёт верхний левый угол — как у
`pdf_whiteout`, чтобы одним словом не переводить потом в макет.
"""
import argparse
import json

MM_PER_PT = 25.4 / 72.0

# Разрыв между глифами, после которого начинается новый span, в долях кегля.
# Кернинг и щели между буквами меньше; интервал — больше (межсловной разрыв
# в Td-расстановке вообще без символа пробела).
PDF_SPANS_GAP_FACTOR = 0.3

# Допуск совпадения базовой линии, пункты: глифы одной строки отличаются
# не больше чем на иную выносную, а разные строки — на межстрочный интервал.
PDF_SPANS_BASELINE_TOL = 0.5


def pdf_spans_get(path: str, page: int = 0) -> list:
    """
    Span'ы страницы: `[{'text','x_mm','y_mm','size_pt','font'}]`.

    Args:
        path: файл PDF.
        page: номер страницы с нуля.

    Returns:
        Список span'ов слов в порядке чтения; `x_mm`, `y_mm` — верхний левый
        угол слова в миллиметрах, `size_pt` — кегль уже с учётом
        масштаба текста, `font` — базовое имя шрифта (у подмножества — с
        префиксом `ABCDEF+`).
    """
    import pypdfium2 as pdfium  # лениво: ядро pdf_ живёт без колёс

    doc = pdfium.PdfDocument(path)
    try:
        pdf_page = doc[page]
        height_pt = pdf_page.get_size()[1]
        textpage = pdf_page.get_textpage()
        spans = []
        current = None
        for i in range(textpage.count_chars()):
            ch = textpage.get_text_range(i, 1)
            if not ch or ch.isspace():
                current = None
                continue
            box = textpage.get_charbox(i, i + 1)      # (left, bottom, right, top)
            obj = textpage.get_textobj(i)
            matrix = obj.get_matrix()
            size = obj.get_font_size() * matrix.d     # до масштаба и в масштабе
            font = obj.get_font().get_base_name()
            if _starts_span(current, font, size, matrix.f, box):
                current = {"text": "", "x_mm": box[0] * MM_PER_PT,
                           "y_mm": (height_pt - box[3]) * MM_PER_PT,
                           "size_pt": size, "font": font, "_baseline": matrix.f,
                           "_right": box[0]}
                spans.append(current)
            current["text"] += ch
            current["_right"] = box[2]
        for s in spans:
            s.pop("_baseline", None)
            s.pop("_right", None)
        return [s for s in spans if s["text"]]
    finally:
        doc.close()


def _starts_span(current, font: float, size: float, baseline: float, box) -> bool:
    """Начинается ли новый span этим символом (иначе он дописывается в текущий)."""
    if current is None:
        return True
    if font != current["font"] or abs(size - current["size_pt"]) > 0.01:
        return True
    if abs(baseline - current["_baseline"]) > PDF_SPANS_BASELINE_TOL:
        return True
    return box[0] - current["_right"] > abs(size) * PDF_SPANS_GAP_FACTOR


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Span\'ы текста PDF: слова с координатами.')
    parser.add_argument('command', choices=['get'])
    parser.add_argument('path', help='файл PDF')
    parser.add_argument('--page', type=int, default=0, help='страница с нуля')
    ns = parser.parse_args()
    for span in pdf_spans_get(ns.path, page=ns.page):
        print(json.dumps(span, ensure_ascii=False))

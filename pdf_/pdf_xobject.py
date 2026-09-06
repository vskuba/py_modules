"""
XObject'ы страницы с их местами: где на самом деле нарисована картинка.

Наивный разбор потока (`/Im\\d+ Do` regex'ом) не знает про `cm`: картинку,
растянутую матрицей `50 0 0 30 100 200 cm`, regex видит квадратом 1×1 pt в
нуле координат — размеры и место врут. Здесь поток обходится операторами
честно: стек `q/Q`, пред-умножение `cm` на текущую матрицу, а `/name Do`
разрешается через `Resources` страницы с учётом наследования.

Наружу — прямоугольник от верхнего левого угла в миллиметрах (как
`pdf_whiteout` и `pdf_spans_get`) и сама матрица: поворот по ней видно
честнее, чем по описывающему прямоугольнику повёрнутой картинки.
"""
import argparse
import json
import re

PDF_XOBJECT_MM_PER_PT = 25.4 / 72.0

# Токены содержимого PDF: `<<` и `>>` — самостоятельные ограничители (Word
# печатает `<</MCID 51 >>BDC` без пробела, и наивный `\S+` слипает `>>BDC` в
# одно слово, теряя оператор BDC); строки и массивы — отдельными кусками,
# остальное — слова без пробелов; имя `/Im1` не склеивается со следующим.
_PDF_XOBJECT_TOKENS = re.compile(
    r"(<<|>>|\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>|\[[^\]]*]|\S+)")


def pdf_xobject_get(path: str, page: int = 0) -> list:
    """
    Картинки страницы с фактическими прямоугольниками.

    Args:
        path: файл PDF.
        page: номер страницы с нуля.

    Returns:
        Список `[{'name','rect_mm': [x,y,w,h],'width_px','height_px',
        'transform': [a,b,c,d,e,f]}]` в порядке отрисовки; `rect_mm` —
        от верхнего левого угла страницы, `width_px`/`height_px` — пиксели
        исходника, `transform` — матрица, с которой картинка легла на
        страницу (единичный квадрат `/Do` умножается на неё). Повёрнутая
        картинка даёт прямоугольник-описание четырёх углов; честный вид —
        в матрице.
    """
    from pypdf import PdfReader  # лениво: ядро pdf_ живёт без колёс

    reader = PdfReader(path)
    page_obj = reader.pages[page]
    height_pt = float(page_obj.mediabox.height)
    xobjects = _resources_xobjects(page_obj)

    contents = page_obj.get_contents()
    data = contents.get_data() if contents is not None else b""

    results = []
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack = []
    operands = []
    for token in _PDF_XOBJECT_TOKENS.findall(data.decode('latin-1', 'replace')):
        if _is_operator(token):
            ctm, stack = _apply(token, operands, ctm, stack, xobjects, results, height_pt)
            operands = []
        else:
            operands.append(token)
    return results


def _resources_xobjects(page_obj) -> dict:
    """XObject'ы страницы ключами без косой черты (как операнд `Do`);
    `Resources` наследуется от родителя, ссылки разрешены."""
    resources = page_obj.get("/Resources")
    if resources is None:
        return {}
    raw = resources.get("/XObject")
    if raw is None:
        return {}
    if hasattr(raw, "get_object"):
        raw = raw.get_object()
    return {str(k).lstrip('/'): v.get_object() if hasattr(v, "get_object") else v
            for k, v in raw.items()}


def _is_operator(token: str) -> bool:
    """Оператор — голое слово: числа, имена `/x`, строки `(...)` — не они."""
    return bool(re.fullmatch(r"[A-Za-z'\"*]+", token))


def _mat_mul(m: list, c: list) -> list:
    """Произведение матриц PDF: точка row-вектором, `m` применяется раньше."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = c
    return [a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2]


def _apply(token: str, operands: list, ctm: list, stack: list, xobjects: dict,
           results: list, height_pt: float) -> tuple:
    """Один оператор поверх состояния обхода; возвращает `(ctm, stack)`."""
    if token == 'q':
        return ctm, stack + [list(ctm)]
    if token == 'Q':
        return (stack[-1] if stack else [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]), stack[:-1]
    if token == 'cm' and len(operands) == 6:
        return _mat_mul([float(v) for v in operands], ctm), stack
    if token == 'Do' and operands:
        image = xobjects.get(operands[0].lstrip('/'))
        if image is not None and str(image.get("/Subtype", "")) == "/Image":
            results.append({
                "name": operands[0].lstrip('/'),
                "rect_mm": _rect_mm(ctm, height_pt),
                "width_px": int(image.get("/Width", 0)),
                "height_px": int(image.get("/Height", 0)),
                "transform": [round(v, 6) for v in ctm],
            })
    return ctm, stack


def _rect_mm(ctm: list, height_pt: float) -> list:
    """Описывающий прямоугольник единичного квадрата после CTM, мм сверху."""
    xs, ys = [], []
    for (ux, uy) in ((0, 0), (0, 1), (1, 0), (1, 1)):
        a, b, c, d, e, f = ctm
        xs.append(a * ux + c * uy + e)
        ys.append(b * ux + d * uy + f)
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return [x0 * PDF_XOBJECT_MM_PER_PT, (height_pt - y1) * PDF_XOBJECT_MM_PER_PT,
            (x1 - x0) * PDF_XOBJECT_MM_PER_PT, (y1 - y0) * PDF_XOBJECT_MM_PER_PT]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='XObject\'ы страницы с местами на листе.')
    parser.add_argument('command', choices=['list'])
    parser.add_argument('path', help='файл PDF')
    parser.add_argument('--page', type=int, default=0, help='страница с нуля')
    ns = parser.parse_args()
    for x in pdf_xobject_get(ns.path, page=ns.page):
        print(json.dumps(x, ensure_ascii=False))

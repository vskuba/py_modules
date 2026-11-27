"""
Контактный лист страниц PDF одной командой — документ глазами за один кадр.

В печатном цикле QA-документов сетку страниц рисовали heredoc'ом каждый раз
заново (19 проверок «что сдвинулось» за одну сессию): `pdf_render` всех
страниц, затем PIL-сетка. `pdf_contact_sheet` = `pdf_render` + `image_sheet`:
рендер страниц — ответственность pdf_, сетка с подписями — `image_`, здесь
только склейка.

Лист — для глаза, а не для чтения: плотность низкая, текст нечитаем — зато
«прыгнувшая» вёрстка, пустая страница и уехавшая картинка видны сразу, без
`pdftotext` по страницам.
"""
import argparse
import json

PDF_SHEET_DPI = 50    # плотность листа: глаз ловит раскладку, чтение не ожидается
PDF_SHEET_COLS = 6    # колонок: A4 на 300 px превью — экран 1900 px почти вровень


def pdf_contact_sheet(pdf_path: str, out_png: str, dpi: int = PDF_SHEET_DPI,
                      cols: int = PDF_SHEET_COLS, thumb: int = 300,
                      pages: list | None = None) -> dict:
    """Страницы PDF сеткой в один PNG; вернуть {'out', 'pages'}.

    Args:
        pdf_path: документ.
        out_png: куда положить лист.
        dpi: плотность растра страниц; больше `thumb` бессмысленно — превью
            ужимается до него.
        cols, thumb: раскладка листа (`image_sheet`).
        pages: номера страниц с 1; пусто — все.

    Returns:
        {'out': путь листа, 'pages': сколько страниц легло}.

    ⚠ Подписи ячеек — имена файлов `page-N.png` из `pdf_render`, отсчёт с 0:
      страница 5 документа — это `page-4.png`.
    """
    import tempfile

    from image_.image_sheet import image_sheet
    from pdf_.pdf_ import pdf_render

    with tempfile.TemporaryDirectory(prefix='pdf-sheet-') as tmp:
        files = pdf_render(pdf_path, tmp, dpi=dpi)
        if pages:
            files = [files[p - 1] for p in pages]
        got = image_sheet(files, str(out_png), cols=cols, thumb=thumb, label='name')
    return {'out': got['out'], 'pages': got['n']}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='python -m pdf_.pdf_sheet',
                                 description='контактный лист страниц PDF')
    ap.add_argument('pdf')
    ap.add_argument('out_png')
    ap.add_argument('--dpi', type=int, default=PDF_SHEET_DPI)
    ap.add_argument('--cols', type=int, default=PDF_SHEET_COLS)
    ap.add_argument('--pages', default='', help='номера с 1 через запятую; пусто — все')
    ns = ap.parse_args()
    pgs = [int(v) for v in ns.pages.split(',')] if ns.pages else None
    print(json.dumps(pdf_contact_sheet(ns.pdf, ns.out_png, dpi=ns.dpi,
                                       cols=ns.cols, pages=pgs), ensure_ascii=False))

"""Тесты `pdf_sheet` — офлайн: PDF из чистых страниц pypdf, рендер pypdfium2."""
import pytest
from PIL import Image
from pypdf import PdfWriter

from pdf_.pdf_sheet import pdf_contact_sheet


def _pdf(tmp_path, n=3):
    path = tmp_path / 'doc.pdf'
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=595, height=842)
    with open(path, 'wb') as fh:
        w.write(fh)
    return str(path)


def test_pdf_contact_sheet_кладёт_все_страницы(tmp_path):
    out = tmp_path / 'sheet.png'
    got = pdf_contact_sheet(_pdf(tmp_path, 3), str(out))
    assert got['pages'] == 3 and out.exists()
    assert Image.open(out).size == (6 * 300, 330)  # cols*thumb, thumb+бар


def test_pdf_contact_sheet_выборка_страниц(tmp_path):
    out = tmp_path / 'two.png'
    got = pdf_contact_sheet(_pdf(tmp_path, 5), str(out), pages=[2, 4])
    assert got['pages'] == 2 and Image.open(out).size == (6 * 300, 330)


def test_pdf_contact_sheet_страница_вне_документа_объясняет_себя(tmp_path):
    with pytest.raises(RuntimeError, match='в нём 3'):
        pdf_contact_sheet(_pdf(tmp_path, 3), str(tmp_path / 'x.png'), pages=[4])
    with pytest.raises(RuntimeError, match='в нём 3'):  # страница 0 — не последняя
        pdf_contact_sheet(_pdf(tmp_path, 3), str(tmp_path / 'x.png'), pages=[0])

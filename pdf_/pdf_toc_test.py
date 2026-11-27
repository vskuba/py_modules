"""Тесты `pdf_toc` — офлайн: печать подменена PDF из pypdf с ручным текстовым слоем."""
import re
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pdf_.pdf_toc import pdf_build_toc, pdf_toc_entries

_RES = DictionaryObject({NameObject('/Font'): DictionaryObject(
    {NameObject('/F1'): DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/BaseFont'): NameObject('/Helvetica')})})})


def _html(entries, filled=()):
    lis = ''.join(
        f'<li class="lvl2"><a href="#{k}"><span class="t">{t}</span></a>'
        f'<span class="dots"></span>'
        f'<span class="pg" data-for="{k}">'
        f'{filled[i] if i < len(filled) else ""}</span></li>'
        for i, (k, t) in enumerate(entries))
    return f'<html><body><ul class="toc">{lis}</ul></body></html>'


def _printer(plan):
    """print_fn: страницы строго по плану, whatever в HTML; план — список текстов."""
    def print_fn(html_path, out_pdf):
        w = PdfWriter()
        for t in plan:
            p = w.add_blank_page(width=612, height=792)
            s = DecodedStreamObject()
            s.set_data(b'BT /F1 12 Tf 40 700 Td (' + t.encode() + b') Tj ET')
            p[NameObject('/Contents')] = s
            p[NameObject('/Resources')] = _RES
        with open(out_pdf, 'wb') as fh:
            w.write(fh)
    return print_fn


def test_pdf_toc_entries_берёт_текст_из_соседнего_t_span(tmp_path):
    got = pdf_toc_entries(_html([('s-a', 'Alpha'), ('s-b', 'Beta Two')]))
    assert got == [{'id': 's-a', 'find': 'Alpha'}, {'id': 's-b', 'find': 'BetaTwo'}]


def test_pdf_toc_entries_data_find_перебивает_t_span():
    html = ('<li><span class="t">Совсем другое</span>'
            '<span class="pg" data-for="x" data-find="Target">3</span></li>')
    assert pdf_toc_entries(html) == [{'id': 'x', 'find': 'Target'}]


def test_pdf_build_toc_проставляет_номера_и_сходится(tmp_path):
    entries = [('s-a', 'Alpha'), ('s-b', 'Beta'), ('s-c', 'Gamma')]
    html = tmp_path / 'doc.html'
    html.write_text(_html(entries), encoding='utf-8')
    # страница 1 — оглавление (все заголовки разом), дальше обложка и контент
    plan = ['Alpha Beta Gamma', 'cover page'] + [t for _, t in entries]
    got = pdf_build_toc(str(html), str(tmp_path / 'doc.pdf'),
                        print_fn=_printer(plan))
    assert got['converged'] and got['rounds'] == 2 and not got['unresolved']
    assert got['pages'] == {'s-a': 3, 's-b': 4, 's-c': 5}
    assert re.search(r'data-for="s-a">3<', html.read_text(encoding='utf-8'))


def test_pdf_build_toc_не_ловит_страницу_оглавления_за_секцию(tmp_path):
    # один и тот же заголовок и в оглавлении, и в контенте — страница с ним
    # одна, и это не та, где больше половины строк
    entries = [('s-a', 'Alpha'), ('s-b', 'Beta'), ('s-c', 'Gamma')]
    html = tmp_path / 'doc.html'
    html.write_text(_html(entries), encoding='utf-8')
    plan = ['Alpha Beta Gamma', 'Alpha plus body'] + [t for _, t in entries[1:]]
    got = pdf_build_toc(str(html), str(tmp_path / 'doc.pdf'),
                        print_fn=_printer(plan))
    assert got['pages']['s-a'] == 2  # контент, а не оглавление


def test_pdf_build_toc_несходимость_видна_в_unresolved(tmp_path):
    entries = [('s-a', 'Alpha'), ('s-z', 'Zeta')]
    html = tmp_path / 'doc.html'
    html.write_text(_html(entries), encoding='utf-8')
    plan = ['Alpha Zeta', 'Alpha body']  # 'Zeta' в контенте не встречается
    got = pdf_build_toc(str(html), str(tmp_path / 'doc.pdf'),
                        print_fn=_printer(plan), rounds=2)
    assert got['unresolved'] == ['s-z'] and got['pages']['s-z'] is None
    assert 'data-for="s-z">?' in html.read_text(encoding='utf-8')

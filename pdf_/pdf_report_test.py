"""Тесты `pdf_report` — офлайн: печать подменена PDF из pypdf, chrome не зовём."""
import json
import os

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from pdf_.pdf_report import _md_html, pdf_report_build, pdf_report_check

# Поток обязан уехать indirect-объектом: pypdf иначе пишет stream внутри
# словаря страницы — pypdf такое читает, а pdfium (растер `pdf_render`) нет.


def _stream(writer, data):
    s = DecodedStreamObject()
    s.set_data(data)
    return writer._add_object(s)


def _page(writer, dense=True):
    """Плотная страница — серый квадрат на всё поле; разряженная — точка в углу."""
    p = writer.add_blank_page(width=842, height=595)
    box = b'0.5 0.5 0.5 rg 0 0 842 595 re f' if dense else b'0 0 0 rg 10 10 6 6 re f'
    p[NameObject('/Contents')] = _stream(writer, box)
    return p


def _page_with_image(writer, tall=False, base_dense=True, tone=b'\x80'):
    """Страница с Image XObject: tall — фигура 500pt (176 mm), выше тела landscape."""
    p = _page(writer, dense=base_dense)
    img = DecodedStreamObject()
    img.set_data(tone * 64)
    img.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                NameObject('/Width'): NumberObject(8), NameObject('/Height'): NumberObject(8),
                NameObject('/ColorSpace'): NameObject('/DeviceGray'),
                NameObject('/BitsPerComponent'): NumberObject(8)})
    xres = DictionaryObject()
    xres[NameObject('/Im0')] = writer._add_object(img)
    res = DictionaryObject()
    res[NameObject('/XObject')] = xres
    p[NameObject('/Resources')] = res
    h = 500 if tall else 100
    p[NameObject('/Contents')] = ArrayObject([
        _stream(writer, b'0.5 0.5 0.5 rg 0 0 842 595 re f' if base_dense else b'0 0 0 rg 10 10 6 6 re f'),
        _stream(writer, f'q 800 0 0 {h} 20 20 cm /Im0 Do Q'.encode())])
    return p


def _printer(*makers):
    def print_fn(html_path, out_pdf):
        w = PdfWriter()
        for make in makers:
            make(w)
        with open(out_pdf, 'wb') as fh:
            w.write(fh)
    return print_fn


def _report_md(tmp_path, body='Данные прогона.\n\n![Deposit](pic.png)\n'):
    from PIL import Image
    Image.new('RGB', (600, 300), 'white').save(str(tmp_path / 'pic.png'))
    md = tmp_path / 'report.md'
    md.write_text(f'# Отчёт 11.27\n\n{body}', encoding='utf-8')
    return md


def test_build_from_markdown_is_clean(tmp_path):
    _report_md(tmp_path)
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'), landscape=True,
                           print_fn=_printer(lambda w: _page(w), lambda w: _page(w)))
    assert got['ok'] and got['issues'] == []
    assert len(got['pages']) == 2 and os.path.isfile(got['sheet'])
    html = open(got['html'], encoding='utf-8').read()
    assert 'A4 landscape' in html and 'max-height: 132mm' in html
    assert '<th>a</th>' not in html                       # таблиц в теле нет
    assert '<figure><img src="pic.png"' in html


def test_md_table_separator_row_not_rendered():
    html = _md_html('| a | b |\n|---|---|\n| 1 | 2 |')
    assert '<th>a</th>' in html and '<td>1</td>' in html and '<th>---</th>' not in html


def test_orphan_page_flagged(tmp_path):
    _report_md(tmp_path)
    out = str(tmp_path / 'r.pdf')
    got = pdf_report_build(str(tmp_path), out,
                           print_fn=_printer(lambda w: _page(w), lambda w: _page(w, dense=False)))
    bad = [i for i in got['issues'] if i['kind'] == 'empty-page']
    assert bad and bad[0]['page'] == 2 and not got['ok']
    # та же страница, пропущенная явным списком, не проблема
    assert pdf_report_check(out, skip_pages=(2,)) == []


def test_white_figure_counts_as_content(tmp_path):
    """Скриншот живой страницы белый: чернил нет, но картинка — содержимое."""
    _report_md(tmp_path)
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'), landscape=True,
                           print_fn=_printer(lambda w: _page_with_image(
                               w, base_dense=False, tone=b'\xff')))
    assert got['ok'] and got['issues'] == []


def test_tall_figure_flagged(tmp_path):
    _report_md(tmp_path)
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'), landscape=True,
                           print_fn=_printer(lambda w: _page_with_image(w, tall=True)))
    kinds = [i['kind'] for i in got['issues']]
    assert 'tall-figure' in kinds and not got['ok']
    # влезшая фигура — чисто
    ok = pdf_report_build(str(tmp_path), str(tmp_path / 'r2.pdf'), landscape=True,
                          print_fn=_printer(lambda w: _page_with_image(w, tall=False)))
    assert ok['ok']


def test_sections_json_source(tmp_path):
    from PIL import Image
    Image.new('RGB', (600, 300), 'white').save(str(tmp_path / 'pic.png'))
    (tmp_path / 'sections.json').write_text(json.dumps(
        {'title': 'R', 'meta': {'date': '2025-11-27'},
         'sections': [{'title': 'A', 'text': 'раз\n\nдва', 'figure': {'src': 'pic.png', 'caption': 'c'}}]},
        ensure_ascii=False), encoding='utf-8')
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'),
                           print_fn=_printer(lambda w: _page(w)))
    html = open(got['html'], encoding='utf-8').read()
    assert got['ok'] and '<th>date</th>' in html and '<figcaption>c</figcaption>' in html


def test_forbid_catches_secret_in_source(tmp_path):
    _report_md(tmp_path, body='Данные.\n\nkey supersecretvalue999\n')
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'),
                           print_fn=_printer(lambda w: _page(w)),
                           forbid=['supersecretvalue999'])
    assert not got['ok'] and got['sweep']['hits']
    hit = got['sweep']['hits'][0]
    assert hit['value'] == 'supersecre'            # наружу — только префикс
    assert 'supersecretvalue999' not in str(got)


def test_html_source_gets_cap_over_author_css(tmp_path):
    (tmp_path / 'report.html').write_text(
        '<html><head><style>img{max-height:172mm}</style></head>'
        '<body><img src="pic.png"></body></html>', encoding='utf-8')
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'), landscape=True,
                           print_fn=_printer(lambda w: _page(w)))
    html = open(got['html'], encoding='utf-8').read()
    assert html.index('max-height:172mm') < html.index('max-height:132mm!important')

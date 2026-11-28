"""Тесты `pdf_report` — офлайн: печать подменена PDF из pypdf, chrome не зовём."""
import json
import os

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from pdf_ import pdf_report as m
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


def _page_with_image(writer, tall=False, base_dense=True, tone=b'\x80', wide=800):
    """Страница с Image XObject: tall — фигура 500pt (176 mm), выше тела landscape;
    wide — ширина рамки в pt (узкая фигура почти не влияет на покрытие)."""
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
        _stream(writer, f'q {wide} 0 0 {h} 20 20 cm /Im0 Do Q'.encode())])
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
    assert ':not([style*="max-height"])' in html  # потолок догоняет те, кто молчит


def test_author_inline_max_height_wins_under_cap(tmp_path):
    """Инлайн `max-height` меньше потолка — сила; больше — зажимается min()."""
    (tmp_path / 'report.html').write_text(
        '<html><head><style>img{max-height:172mm}</style></head><body>'
        '<figure><img src="pic.png" style="max-height:60mm">'
        '<img src="pic.png" style="max-height:300mm"></body></html>', encoding='utf-8')
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'), landscape=True,
                           print_fn=_printer(lambda w: _page(w)))
    html = open(got['html'], encoding='utf-8').read()
    assert 'max-height:min(132mm,60mm)' in html      # автор просил меньше — меньшее и стоит
    assert 'max-height:min(132mm,300mm)' in html     # просил больше — потолок поверх
    assert '{max-height:132mm!important}' in html    # молчащих закрывает общее правило


def test_cover_page_exempted_in_build(tmp_path):
    """Обложка без чернил — не провал сборки: skip_pages (он же --cover) доходит до проверок."""
    _report_md(tmp_path)
    makers = (lambda w: _page(w, dense=False), lambda w: _page(w))
    got = pdf_report_build(str(tmp_path), str(tmp_path / 'r.pdf'), landscape=True, print_fn=_printer(*makers))
    bad = [i for i in got['issues'] if i['kind'] == 'empty-page']
    assert bad and bad[0]['page'] == 1 and not got['ok']
    ok = pdf_report_build(str(tmp_path), str(tmp_path / 'r2.pdf'), landscape=True, skip_pages=(1,),
                          print_fn=_printer(*makers))
    assert ok['ok'] and ok['issues'] == []


def test_cli_cover_and_skip_page_reach_build(monkeypatch):
    seen = {}

    def fake_build(src, out, **kw):
        seen.update(kw)
        return {'pdf': out, 'pages': [], 'sheet': 'sheet.png', 'issues': [], 'sweep': None, 'ok': True}

    monkeypatch.setattr(m, 'pdf_report_build', fake_build)
    with pytest.raises(SystemExit) as stop:
        m._cli(['build', 'src', '--out', 'x.pdf', '--cover', '--skip-page', '3'])
    assert stop.value.code == 0
    assert sorted(seen['skip_pages']) == [1, 3]


def test_cli_figure_max_height_alias(monkeypatch):
    seen = {}

    def fake_build(src, out, **kw):
        seen.update(kw)
        return {'pdf': out, 'pages': [], 'sheet': 'sheet.png', 'issues': [], 'sweep': None, 'ok': True}

    monkeypatch.setattr(m, 'pdf_report_build', fake_build)
    with pytest.raises(SystemExit):
        m._cli(['build', 'src', '--out', 'x.pdf', '--figure-max-height', '120'])
    assert seen['figure_mm'] == 120.0


def test_empty_page_reason_names_the_layout(tmp_path):
    """Провал по пустоте обязан сказать, что именно править: хвост, свободное место, фигуру."""
    _report_md(tmp_path)
    out = str(tmp_path / 'r.pdf')
    pdf_report_build(str(tmp_path), out, landscape=True,
                     print_fn=_printer(lambda w: _page(w), lambda w: _page(w, dense=False)))
    bad = [i for i in pdf_report_check(out) if i['kind'] == 'empty-page'][0]
    assert 'хвост' in bad['detail'] and 'фигур впереди нет' in bad['detail']
    assert bad['free_mm'] > 0


def test_empty_page_reason_points_to_the_figure(tmp_path):
    _report_md(tmp_path)
    got = pdf_report_build(
        str(tmp_path), str(tmp_path / 'r.pdf'), landscape=True,
        print_fn=_printer(lambda w: _page(w), lambda w: _page(w, dense=False),
                          lambda w: _page_with_image(w, tall=True, base_dense=False,
                                                     tone=b'\xff', wide=60)))
    bad = [i for i in got['issues'] if i['kind'] == 'empty-page' and i['page'] == 2][0]
    assert 'следующая фигура' in bad['detail'] and 'не влезла' in bad['detail']
    assert bad['figure_mm'] > 130

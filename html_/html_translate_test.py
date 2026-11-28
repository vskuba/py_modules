"""Тесты `html_translate` — офлайн: модель подменена, бинарь gx10 не зовём (кроме формы вызова)."""
import os

import pytest

from html_ import html_translate as m
from html_.html_translate import HTML_TRANSLATE_SPLIT, html_translate, html_translate_html


def _doc(body=''):
    return ('<!doctype html><html lang="en"><head><style>img{max-height:132mm}</style>'
            '<title>Report</title></head><body>' + body + '</body></html>')


SAMPLE = _doc('<p>Deposit flow</p><h2>Overview</h2>'
              '<p>Cashier <code>payCurrency</code> line <img src="shot.png">'
              '<ul><li>PASS</li></ul><h2>Results</h2><p>Summary line</p>')


def test_head_style_untouched_sections_translated():
    got = html_translate_html(SAMPLE, call=lambda t, s: t.replace('Overview', 'Обзор'))
    head = got['html'].split('<body')[0]
    assert '<style>img{max-height:132mm}</style>' in head and '<title>Report</title>' in head
    assert 'Обзор' in got['html'] and got['chunks'] == 3 and got['retried'] == []
    assert '<p>Deposit flow</p>' in got['html']      # преамбула до первого h2 цела


def test_chunks_split_before_each_h2():
    seen = []
    html_translate_html(SAMPLE, call=lambda t, s: seen.append(t) or t)
    assert len(seen) == 3
    assert seen[0].startswith('<p>Deposit') and seen[1].startswith('<h2>') and seen[2].startswith('<h2>')
    assert not HTML_TRANSLATE_SPLIT.search('<h2x>ok</h2x>')  # заголовок-двойник не режется


def test_tag_mismatch_retried_once_then_ok():
    calls = []

    def mock(text, system):
        calls.append(text)
        return text.replace('</ul>', '') if len(calls) == 2 else text

    got = html_translate_html(SAMPLE, call=mock)
    assert len(calls) == 4 and got['retried'] == [2]  # второй кусок — с <ul>


def test_persistent_mismatch_fails_loudly():
    with pytest.raises(RuntimeError, match='потерей разметки'):
        html_translate_html(SAMPLE, call=lambda t, s: t.replace('<li>PASS</li>', '<li>PASS'))


def test_img_count_guard():
    with pytest.raises(RuntimeError, match='img'):
        html_translate_html(SAMPLE, call=lambda t, s: t.replace('<p>', '<p><img src="x.png">'))


def test_glossary_and_keep_reach_the_prompt():
    systems = []
    html_translate_html(SAMPLE, call=lambda t, s: systems.append(s) or t,
                        glossary={'cashier': 'кассовый сервис'}, keep=['HeroPayments'])
    assert 'кассовый сервис' in systems[0] and 'HeroPayments' in systems[0]
    assert 'Russian' in systems[0] and 'English' in systems[0]


def test_html_lang_switched_to_target():
    got = html_translate_html(SAMPLE, call=lambda t, s: t)
    head = got['html'].split('<body')[0]
    assert 'lang="ru"' in head and 'lang="en"' not in head


def test_body_without_closing_tag_survives():
    bodyless = SAMPLE.replace('</body>', '')
    got = html_translate_html(bodyless, call=lambda t, s: t)
    assert got['html'] == bodyless.replace('lang="en"', 'lang="ru"')


def test_no_body_raises():
    with pytest.raises(ValueError, match='body'):
        html_translate_html('<html><head></head></html>', call=lambda t, s: t)


def test_gx10_call_shape_and_fence_stripped(monkeypatch):
    def fake_run(cmd, input=None, capture_output=True, text=True, timeout=None):
        assert cmd[0].endswith('gx10') and '-s' in cmd and 'Translate' in cmd[-1]
        assert input.startswith('<p>')

        class Proc:
            stdout, stderr = '```html\n<p>текст</p>\n```', ''

        return Proc()

    monkeypatch.setattr(m.subprocess, 'run', fake_run)
    assert m.html_translate_gx10('<p>text</p>', 'SYS') == '<p>текст</p>'

    class Empty:
        stdout, stderr = '', 'boom'

    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **k: Empty())
    with pytest.raises(RuntimeError, match='пустой'):
        m.html_translate_gx10('<p>text</p>', 'SYS')


def test_glossary_file_both_flavours(tmp_path):
    (tmp_path / 'terms.json').write_text('{"cashier": "кассовый сервис"}', encoding='utf-8')
    assert m._read_glossary(str(tmp_path / 'terms.json')) == {'cashier': 'кассовый сервис'}
    (tmp_path / 'terms.txt').write_text('cashier = кассовый сервис\n# комментарий\n', encoding='utf-8')
    assert m._read_glossary(str(tmp_path / 'terms.txt')) == {'cashier': 'кассовый сервис'}


def test_file_level_default_name(tmp_path):
    src = tmp_path / 'report.html'
    src.write_text(SAMPLE, encoding='utf-8')
    got = html_translate(str(src), lang='ru', call=lambda t, s: t.replace('Summary', 'Сводка'))
    assert got['out'].endswith('report.ru.html') and os.path.isfile(got['out'])
    assert 'Сводка' in open(got['out'], encoding='utf-8').read()


def test_cli_passes_flags(monkeypatch, capsys):
    seen = {}

    def fake_translate(src_file, out_file='', **kw):
        seen.update(kw, src_file=src_file)
        return {'html': '', 'chunks': 3, 'retried': [], 'out': 'report.ru.html'}

    monkeypatch.setattr(m, 'html_translate', fake_translate)
    assert m._cli(['report.html', '--lang', 'ru', '--glossary', 'terms.txt',
                   '--keep', 'HeroPayments']) == 0
    assert seen['glossary_file'] == 'terms.txt' and seen['keep'] == ['HeroPayments']
    assert 'кусков 3' in capsys.readouterr().out

"""Тесты `html_report` — чистый генератор, офлайн без браузера и печати."""
import re

from html_.html_report import html_report
from pdf_.pdf_toc import pdf_toc_entries

SPEC = {'title': 'QA "Draft" 26.11', 'meta': 'Cashier · v2',
        'lead': '<p>Зачем</p>', 'legend': '<table><tr><td>y</td></tr></table>',
        'parts': [{'id': 'p1', 'num': 'I', 'title': 'Current state', 'sub': 'что есть',
                   'sections': [{'id': 's-a', 'num': '1', 'title': 'Numbers',
                                 'body': '<p>1</p>', 'page': True},
                                {'id': 's-b', 'num': '2', 'title': 'Gaps',
                                 'body': '<p>2</p>'}]}]}


def test_html_report_рисует_пустые_номера_оглавления():
    html = html_report(SPEC)
    assert '<span class="pg" data-for="p1"></span>' in html
    assert '<span class="pg" data-for="s-b"></span>' in html


def test_html_report_оглавление_совпадает_с_контрактом_pdf_toc():
    entries = pdf_toc_entries(html_report(SPEC))
    assert [e['id'] for e in entries] == ['p1', 's-a', 's-b']
    assert entries[1]['find'] == '1.Numbers'  # тот же текст, что в h3 секции


def test_html_report_секция_с_page_начинается_с_новой_страницы():
    html = html_report(SPEC)
    assert '<div class="page"><section id="s-a">' in html
    assert '<section id="s-b">' in html and '<div class="page"><section id="s-b"' not in html


def test_html_report_заголовок_в_колонтитуле_экранирован():
    html = html_report(SPEC)
    assert 'content:"QA \\"Draft\\" 26.11"' in html


def test_html_report_якоря_оглавления_ведут_в_существующие_id():
    # битая ссылка в оглавлении — PDF с цифрой, которая ведёт в никуда
    html = html_report(SPEC)
    hrefs = set(re.findall(r'href="#([^"]+)"', html))
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    assert hrefs == {'p1', 's-a', 's-b'} and hrefs <= ids

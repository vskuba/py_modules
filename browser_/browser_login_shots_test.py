"""Тесты PII-скана `browser_login_shots` — офлайн, на тексте без браузера."""
from browser_.browser_login_shots import (
    _by_file,
    browser_login_shots_pii,
    browser_login_shots_pii_report,
)


def test_pii_находит_почту_айпи_карту_и_маску():
    text = ('operator: admin@pay.example.com, client 10.11.12.13, '
            'card 4242424242424242, mask 424242******4242, cid 90210')
    hits = browser_login_shots_pii(text)
    assert {'email', 'ipv4', 'pan', 'masked_pan'} <= {h['kind'] for h in hits}
    assert all(len(h['value']) <= 10 for h in hits)  # полные значения — никогда


def test_pii_свой_паттерн_дополняет_стандартные():
    hits = browser_login_shots_pii('cid 90210', {'cid': r'\bcid\s+\d+'})
    assert hits == [{'kind': 'cid', 'value': 'cid 90210'}]  # 9 символов — целиком
    hits = browser_login_shots_pii('cid 90210111213', {'cid': r'\bcid\s+\d+'})
    assert hits[0]['value'] == 'cid 902101'                 # длиннее — срезано по 10


def test_pii_report_пишет_строки_без_значений_целиком(tmp_path):
    path = browser_login_shots_pii_report(
        str(tmp_path), {'a.png': [{'kind': 'email', 'value': 'admin@pay'}],
                        'b.png': []})
    body = open(path, encoding='utf-8').read()
    assert 'a.png: email «admin@pay»' in body and 'b.png' not in body


def test_pii_report_чисто_значит_нет_файла(tmp_path):
    assert browser_login_shots_pii_report(str(tmp_path), {'a.png': []}) == ''
    assert not (tmp_path / 'pii-report.md').exists()


def test_by_file_режет_аргументы_на_владельца_и_значение():
    assert _by_file(['a.png|sel', 'body .email']) == {'a.png': ['sel'], '*': ['body .email']}

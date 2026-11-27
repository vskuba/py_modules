"""Тесты `http_card` — офлайн: собираем HTML, playwright не нужен (метки проверяем по якорям)."""
import pytest

from http_.http_card import (HTTP_CARD_DOTS, _find_line, _parse_path, http_card_html,
                             http_card_mask, _sweep_html)

SPEC = {
    'title': 'Refund 10.00 EUR', 'method': 'POST',
    'url': 'https://api.example.test/v2/payments?signature=deadbeefcafe1234&account=123',
    'request_headers': {'Authorization': 'Bearer abcdefghijkl', 'Content-Type': 'application/json',
                        'API_TOKEN': 'zzz999888777'},
    'request_body': {'amount': 500, 'currency': 'EUR', 'API_KEY': 'secretkey123',
                     'card_number': '4111111111111111',
                     'continue_request': {'uri': 'https://hps.example.test/go?signature=abc123def'}},
    'status': 200,
    'response_body': {'id': 'pay_1', 'fiat_data': {'amount': 1000, 'currency': 'EUR'},
                      'description': 'Refund total'},
    'caption': 'stage',
    'marks': [
        {'path': '$.fiat_data.amount', 'label': 'amount in cents'},        # 0, response
        {'path': 'response.$.description', 'label': 'comment'},            # 1
        {'path': 'request.$.amount', 'label': 'request amount'},           # 2
        {'path': 'request.$.continue_request.uri', 'label': 'sig inside'}, # 3
        {'path': 'headers.$.Authorization', 'label': 'bearer'},            # 4
        {'path': 'url.$.signature', 'label': 'url signature'},             # 5
        {'path': 'response.$.nope', 'label': 'miss'},                      # 6, мимо
    ],
}


def _anchored(html, number):
    start = html.find(f'data-m="{number}"')
    assert start != -1, f'якорь {number} не проставлен'
    begin = html.rfind('>', 0, start) + 1
    end = html.find('</span>', start)
    return html[begin:end]


def test_http_card_html_masks_keys_ignoring_case():
    html = http_card_html(SPEC)
    assert 'zzz999888777' not in html and 'secretkey123' not in html
    assert HTTP_CARD_DOTS in html


def test_http_card_html_masks_signature_in_url_and_strings():
    html = http_card_html(SPEC)
    assert 'deadbeefcafe1234' not in html       # query в адресной строке
    assert 'abc123def' not in html              # signature внутри continue_request.uri
    assert 'account=123' in html                # не секрет — цел


def test_http_card_html_pan_masked_to_last4():
    html = http_card_html(SPEC)
    assert '4111111111111111' not in html
    assert '1111' in html                       # хвост виден


def test_http_card_html_marks_follow_json_path_not_order():
    html = http_card_html(SPEC)
    # на обеих сторонах есть amount: метка request идёт в запрос, не в ответ
    assert '500' in _anchored(html, 2) and 'fiat_data' not in _anchored(html, 2)
    assert '1000' in _anchored(html, 0)          # $.fiat_data.amount — ответ
    assert 'description' in _anchored(html, 1)
    assert 'uri' in _anchored(html, 3)
    assert 'Authorization' in _anchored(html, 4)  # заголовок, регистр имени любой
    assert 'signature=' in _anchored(html, 5)     # диапазон внутри url
    assert 'data-m="6"' not in html               # мимо — якоря нет


def test_http_card_mask_cleans_values_by_key_name():
    got = http_card_mask({'AUTHORIZATION': 'Bearer x', 'nested': {'Api_Key': 'k'*8, 'ok': 1}})
    assert got['AUTHORIZATION'] == HTTP_CARD_DOTS
    assert got['nested']['Api_Key'] == HTTP_CARD_DOTS
    assert got['nested']['ok'] == 1


def test_sweep_catches_value_before_png_write():
    html = http_card_html(SPEC) + 'deadbeefcafe1234'
    with pytest.raises(RuntimeError, match='запрещённые значения'):
        _sweep_html(html, ['deadbeefcafe1234'])
    _sweep_html(http_card_html(SPEC), ['nothing-here'])  # чисто — не бросает


def test_parse_path_sides_and_steps():
    assert _parse_path('response.$.fiat_data.amount', 0)[0] == 'response'
    assert _parse_path('$.items[2].id', 1)[0] == ''  # голый путь — сторона на разрешении
    assert _parse_path('headers.$.Authorization', 2)[0] == 'headers'
    assert _parse_path('$.items[2].id', 1)[1] == ('items', 2, 'id')  # индекс — число


def test_find_line_by_tuple_and_header_name():
    lines = [('    "amount": 1000,', ('fiat_data', 'amount'))]
    assert _find_line(lines, ('fiat_data', 'amount')) == 0
    assert _find_line(lines, ('fiat_data', 'nope')) is None
    heads = [('Authorization: ••••••', ('authorization',))]
    assert _find_line(heads, ('Authorization',), lower=True) == 0

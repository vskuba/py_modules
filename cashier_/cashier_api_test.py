"""Тесты внешнего API: путь вызова, подпись callback-а и ответ без исключений."""
import hashlib
import hmac
import io
import json
import urllib.error
from pathlib import Path

import pytest
import cashier_.cashier_api as cashier_api
from cashier_.cashier_api import (_cashier_api_body, _cashier_api_operation, _cashier_api_path,
                                  _cashier_api_target, cashier_api_call, cashier_api_signature)

BODY = b'{"payment_id":"abc"}'


def test_cashier_api_path_adds_version_prefix():
    """Короткий путь дополняется версией, а готовый остаётся как есть: `v2` руками."""
    assert _cashier_api_path('refund/payment-methods') == '/api/v1/refund/payment-methods'
    assert _cashier_api_path('/api/v2/refund/payment-methods') == '/api/v2/refund/payment-methods'


def test_cashier_api_target_reads_operation_and_path():
    """CLI принимает и операцию, и путь: `refund` разворачивается в вызов методов."""
    assert _cashier_api_target('refund') == '/api/v1/refund/payment-methods'
    assert _cashier_api_target('payout', 'payment-process') == '/api/v1/payout/payment-process'
    assert _cashier_api_target('/api/v1/deposit/payment-methods') == '/api/v1/deposit/payment-methods'


def test_cashier_api_operation_of_typo_is_named():
    """Опечатка в операции называется списком, а не молча даёт 404 на пустом пути."""
    assert _cashier_api_operation('payout') == 'payout'
    with pytest.raises(ValueError) as bad:
        _cashier_api_operation('whitdraw')
    assert 'refund' in str(bad.value)


def test_cashier_api_signature_is_hmac_sha512_of_raw_body():
    """Подпись callback-а — `hash_hmac('sha512', body, key)` по сырым байтам тела."""
    assert cashier_api_signature(BODY, 'k3y') == hmac.new(b'k3y', BODY, hashlib.sha512).hexdigest()
    assert cashier_api_signature(BODY.decode(), 'k3y') == cashier_api_signature(BODY, 'k3y')
    assert len(cashier_api_signature('{}', 'k3y')) == 128


def test_cashier_api_body_reads_file_or_literal(tmp_path: Path):
    """Тело берут из файла (`@файл`) или строкой; файл важен байтами, JSON — для черновика."""
    path = tmp_path / 'body.json'
    path.write_text(json.dumps({'amount': 100}), encoding='utf-8')
    assert _cashier_api_body(f'@{path}') == {'amount': 100}
    assert _cashier_api_body('{"amount": 100}') == {'amount': 100}
    with pytest.raises(ValueError):
        _cashier_api_body('{не json')


def test_cashier_api_call_returns_error_answer_as_data(monkeypatch):
    """4xx — это ответ, а не исключение: `ok=False`, статус и текст видны человеку."""
    def fake(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 422, 'Unprocessable', {},
                                     io.BytesIO(b'{"errors":["amount too low"]}'))

    monkeypatch.setattr(cashier_api.urllib.request, 'urlopen', fake)
    got = cashier_api_call('/api/v1/refund/payment-methods', {}, token='t', timeout=1)
    assert got['ok'] is False and got['status'] == 422
    assert got['response'] == {'errors': ['amount too low']}


def test_cashier_api_call_adds_bearer_and_idempotency(monkeypatch):
    """С заголовками уходят Bearer и `X-Idempotency-Key`: повтор не делает второй платёж."""
    seen = {}

    class Reader(io.BytesIO):
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake(request, timeout=0):
        seen['headers'] = dict(request.headers)
        seen['data'] = request.data
        return Reader(b'{"payment_id":"p-1"}')

    monkeypatch.setattr(cashier_api.urllib.request, 'urlopen', fake)
    got = cashier_api_call('refund/payment-methods', {'amount': 10}, token='secret-token',
                           idempotency='abc', env='stage', timeout=1)
    assert seen['headers']['Authorization'] == 'Bearer secret-token'
    assert seen['headers'].get('X-idempotency-key') == 'abc'
    assert json.loads(seen['data']) == {'amount': 10}
    assert got['ok'] is True and got['url'].startswith('https://api.stage-ipaygateway.com')

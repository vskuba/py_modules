"""Тесты GraphQL-сессии дашборда: файл сессии, разбор ошибок и выбор бренда."""
from pathlib import Path

import pytest
from cashier_.cashier_dashboard import (CASHIER_DASHBOARD_ACCOUNTS, _cashier_dashboard_errors,
                                        _cashier_dashboard_is_expired, _cashier_dashboard_pick,
                                        _cashier_dashboard_store_path, _cashier_dashboard_store_read,
                                        _cashier_dashboard_store_write)


def test_cashier_dashboard_store_path_named_by_env(tmp_path: Path):
    """Файл сессии называется средой: две среды не затирают cookie друг друга."""
    assert _cashier_dashboard_store_path('stage').name == 'dashboard-stage.json'
    assert str(_cashier_dashboard_store_path('dev', store=str(tmp_path / 's.json'))).endswith('s.json')


def test_cashier_dashboard_store_roundtrip_keeps_account(tmp_path: Path):
    """Сессия переживает перезапуск процесса: cookie, бренд и свежий флаг — из файла."""
    saved = {'env': 'stage', 'cookies': {'SESSID': 'abc'},
             'account': {'uuid': 'u-1', 'name': 'WC1'}, 'fresh': False}
    _cashier_dashboard_store_write(saved, store=str(tmp_path / 's.json'))
    read = _cashier_dashboard_store_read('stage', store=str(tmp_path / 's.json'))
    assert read['cookies'] == {'SESSID': 'abc'} and read['account']['name'] == 'WC1'


def test_cashier_dashboard_store_file_is_private(tmp_path: Path):
    """Файл с cookie и паролем доступен только владельцу: права ставят 600."""
    path = tmp_path / 's.json'
    _cashier_dashboard_store_write({'env': 'stage', 'cookies': {'SESSID': 'abc'}}, store=str(path))
    assert path.stat().st_mode & 0o777 == 0o600


def test_cashier_dashboard_store_read_of_missing_file_is_empty(tmp_path: Path):
    """Ни сессии, ни файла — это пустой словарь, а не ошибка: вход сделается сам."""
    assert _cashier_dashboard_store_read('stage', store=str(tmp_path / 'нет.json')) == {}


def test_cashier_dashboard_errors_join_messages():
    """GraphQL-ошибки сворачиваются в одну строку — её показывают человеку."""
    got = {'errors': [{'message': 'Cannot query field "x"'}, {'message': 'второй'}]}
    assert _cashier_dashboard_errors(got) == 'Cannot query field "x"; второй'


def test_cashier_dashboard_is_expired_reads_auth_signals():
    """Просроченную сессию узнают по signs из ответа, чтобы войти заново и повторить."""
    assert _cashier_dashboard_is_expired({'errors': [{'message': 'token is expired'}]})
    assert _cashier_dashboard_is_expired({'errors': [{'message': 'UNAUTHENTICATED: bad token'}]})
    assert _cashier_dashboard_is_expired({'errors': [{'message': 'Session not found'}]})
    assert not _cashier_dashboard_is_expired({'errors': [{'message': 'ошибка валидации'}]})
    assert not _cashier_dashboard_is_expired({'data': {'Accounts': []}})


def test_cashier_dashboard_pick_by_uuid_then_name():
    """Бренд ищут сначала по uuid, потом по имени: регистр имени не важен."""
    accounts = [{'uuid': 'A-1', 'name': 'WC1'}, {'uuid': 'B-2', 'name': 'Casino'}]
    assert _cashier_dashboard_pick('b-2', accounts)['name'] == 'Casino'
    assert _cashier_dashboard_pick('A-1', accounts)['name'] == 'WC1'


def test_cashier_dashboard_pick_lists_available_brands():
    """Бренда нет в доступе — ошибка перечисляет те, что есть, а не молчит."""
    with pytest.raises(RuntimeError) as bad:
        _cashier_dashboard_pick('Nodada', [{'uuid': 'A-1', 'name': 'WC1'}])
    assert 'WC1' in str(bad.value) and 'Nodada' in str(bad.value)


def test_cashier_dashboard_accounts_query_has_no_current_account():
    """Список брендов — `Accounts`: на корне Query своего `currentAccount` нет."""
    assert 'Accounts' in CASHIER_DASHBOARD_ACCOUNTS and 'currentAccount' not in CASHIER_DASHBOARD_ACCOUNTS

"""Тесты бренда: uuid, имя файла кэша и что тёплый кэш не дёргает ни базу, ни OAuth."""
import time
from pathlib import Path

import pytest
import cashier_.cashier_brand as cashier_brand
from cashier_.cashier_brand import (_cashier_brand_key, _cashier_brand_looks_uuid,
                                    _cashier_brand_store_path, cashier_brand_token)

UUID = '9cc67582-9be0-475a-b7f3-2637193806ee'


def test_cashier_brand_looks_uuid_accepts_only_uuid():
    """Имя бренда от uuid отличается однозначно: `WC1` ищут по имени, а не по uuid."""
    assert _cashier_brand_looks_uuid(UUID)
    assert not _cashier_brand_looks_uuid('WC1')
    assert not _cashier_brand_looks_uuid('')
    assert not _cashier_brand_looks_uuid('9cc67582-9be0-475a-b7f3')


def test_cashier_brand_key_folds_case():
    """Регистр имени не множит файлы кэша: `WC1` и `wc1` — один бренд."""
    assert _cashier_brand_key(' WC1 ') == 'wc1' and _cashier_brand_key('') == ''


def test_cashier_brand_store_path_per_env_and_brand(tmp_path: Path):
    """Кэш токена — отдельный файл на бренд и среду: два бренда не делят Bearer."""
    assert _cashier_brand_store_path('stage', 'wc1').name == 'token-stage-wc1.json'
    assert _cashier_brand_store_path('dev', UUID).name == f'token-dev-{UUID}.json'


def test_cashier_brand_store_file_is_private(tmp_path: Path):
    """Файл с Bearer-токеном доступен только владельцу: права 600, каталог 700."""
    cashier_brand._cashier_brand_store_write({'env': 'stage', 'brand': UUID,
                                             'access_token': 'abcd'}, store=str(tmp_path / 't.json'))
    assert (tmp_path / 't.json').stat().st_mode & 0o777 == 0o600


def test_cashier_brand_token_of_live_cache_skips_database(tmp_path: Path, monkeypatch):
    """Тёплый кэш отвечает без единого запроса: ни базы, ни OAuth — файл и всё."""
    monkeypatch.setattr(cashier_brand, 'cashier_brand_uuid', _boom)
    monkeypatch.setattr(cashier_brand, '_cashier_brand_fetch_token', _boom)
    _save(tmp_path, 'cached', lives=3000)
    got = cashier_brand_token('WC1', env='stage', store=str(tmp_path / 't.json'))
    assert got['access_token'] == 'cached'


def test_cashier_brand_token_near_expiry_is_fetched_anew(tmp_path: Path, monkeypatch):
    """До конца срока меньше запаса — токен берут заново, просроченный не отдают."""
    calls = []
    monkeypatch.setattr(cashier_brand, 'cashier_brand_uuid', lambda *a, **k: UUID)
    monkeypatch.setattr(cashier_brand, 'cashier_brand_creds',
                        lambda *a, **k: {'username': 'u', 'password': 'p'})
    monkeypatch.setattr(cashier_brand, '_cashier_brand_fetch_token',
                        lambda *a, **k: calls.append(a) or _token('fresh'))
    _save(tmp_path, 'old', lives=10)
    got = cashier_brand_token('WC1', env='stage', store=str(tmp_path / 't.json'))
    assert calls and got['access_token'] == 'fresh'


def test_cashier_brand_token_fresh_flag_ignores_cache(tmp_path: Path, monkeypatch):
    """`--fresh` не смотрит в файл: сменили креды бренда — старый токен уже не токен."""
    calls = []
    monkeypatch.setattr(cashier_brand, 'cashier_brand_uuid', lambda *a, **k: UUID)
    monkeypatch.setattr(cashier_brand, 'cashier_brand_creds',
                        lambda *a, **k: {'username': 'u', 'password': 'p'})
    monkeypatch.setattr(cashier_brand, '_cashier_brand_fetch_token',
                        lambda *a, **k: calls.append(a) or _token('fresh'))
    _save(tmp_path, 'cached', lives=3000)
    got = cashier_brand_token('WC1', env='stage', store=str(tmp_path / 't.json'), fresh=True)
    assert calls[0] == (UUID, 'u', 'p') and got['access_token'] == 'fresh'


def test_cashier_brand_token_without_access_token_is_error(tmp_path, monkeypatch):
    """Ответ OAuth без `access_token` — ошибка с текстом ответа, а не пустый Bearer."""
    monkeypatch.setattr(cashier_brand, 'cashier_brand_uuid', lambda *a, **k: UUID)
    monkeypatch.setattr(cashier_brand, 'cashier_brand_creds',
                        lambda *a, **k: {'username': 'u', 'password': 'p'})
    monkeypatch.setattr(cashier_brand.urllib.request, 'urlopen',
                        lambda *a, **k: _Reader(b'{"error_description":"invalid_grant"}'))
    with pytest.raises(RuntimeError) as bad:
        cashier_brand_token(UUID, env='stage', store=str(tmp_path / 't.json'), fresh=True)
    assert 'invalid_grant' in str(bad.value)


def _boom(*args, **kwargs):
    raise AssertionError('посторонний вызов: тёплый кэш не должен ходить ни в базу, ни в OAuth')


def _token(access: str, lives: int = 3600) -> dict:
    return {'env': 'stage', 'brand': UUID, 'access_token': access, 'token_type': 'Bearer',
            'expires_in': lives, 'obtained': int(time.time()),
            'expires': int(time.time()) + lives}


def _save(tmp_path: Path, access: str, lives: int) -> None:
    cashier_brand._cashier_brand_store_write(_token(access, lives), store=str(tmp_path / 't.json'))


class _Reader:
    """Замена ответа urllib: один файл-тело, без сети."""

    def __init__(self, payload: bytes):
        self.payload, self.status = payload, 400

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

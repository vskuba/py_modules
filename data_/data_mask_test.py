"""Тесты маски секретов в JSON-дампах и sweep по артефактам."""
from data_.data_mask import (data_mask_json, data_mask_sweep, data_mask_sweep_text,
                             data_mask_values)


def test_data_mask_json_camel_case_key_masked():
    """Ключ из списка в camelCase маскируется: сравнение — без учёта регистра."""
    masked, err = data_mask_json({'cardExpireDate': '3024'})
    assert err is None
    assert masked['cardExpireDate'] == '****'


def test_data_mask_json_oauth_tokens_masked():
    """OAuth-ответ (GraphQL `PspToken`): access/refresh-токены и пароль не уходят наружу."""
    dump = {'data': {'PspToken': {'accessToken': 'e56a7a3c28230bafc6712e7b4641cc3f',
                                  'refreshToken': 'a6f71ad03a278acbcbb9ece6b795c1c0',
                                  'expiresIn': 3600, 'tokenType': 'bearer'}},
            'login': {'password': 'hunter22', 'access_token': 'abcdef0123456789'}}
    masked, err = data_mask_json(dump)
    assert err is None
    token = masked['data']['PspToken']
    assert set(token['accessToken']) == {'*'}
    assert set(token['refreshToken']) == {'*'}
    assert token['expiresIn'] == 3600 and token['tokenType'] == 'bearer'
    assert set(masked['login']['password']) == {'*'}
    assert set(masked['login']['access_token']) == {'*'}


def test_data_mask_json_plain_fields_untouched():
    """Обычные поля не трогаются: маска режет секреты, а не всё подряд."""
    masked, err = data_mask_json({'alias': 'Provider', 'status': 'PENDING', 'amount': 200})
    assert err is None
    assert masked == {'alias': 'Provider', 'status': 'PENDING', 'amount': 200}


def test_data_mask_json_short_key_names_masked():
    """`token`/`api_token`/`encryption_key`/`signature` из конфигов контуров замазаны."""
    masked, err = data_mask_json({'token': 't' * 24, 'api_token': 'a' * 16,
                                  'ENCRYPTION_KEY': 'k' * 12, 'Signature': 's' * 32,
                                  'login': 'ops@example.test'})
    assert err is None
    for key in ('token', 'api_token', 'ENCRYPTION_KEY', 'Signature'):
        assert set(masked[key]) == {'*'}
    assert masked['login'] == 'ops@example.test'


def test_sweep_text_finds_and_truncates_value():
    hits, err = data_mask_sweep_text('x deadbeefcafe1234 y deadbeefcafe1234 z',
                                     ['deadbeefcafe1234', 'clean'])
    assert err is None
    assert hits == [{'value': 'deadbeefca', 'count': 2}]   # наружу — только префикс


def test_sweep_text_empty_values_is_error():
    hits, err = data_mask_sweep_text('что угодно', [])
    assert hits is None and 'пуст' in err


def test_sweep_files_and_trees(tmp_path):
    (tmp_path / 'card.json').write_text('{"note": "deadbeefcafe1234"}', encoding='utf-8')
    nested = tmp_path / 'out'
    nested.mkdir()
    (nested / 'page.html').write_text('<p>чисто</p>', encoding='utf-8')
    got, err = data_mask_sweep([str(tmp_path)], ['deadbeefcafe1234', 'clean'])
    assert err is None and got['scanned'] == 2
    assert got['hits'] == [{'value': 'deadbeefca', 'count': 1, 'file': str(tmp_path / 'card.json')}]
    # тот же файл, названный и прямо, и каталогом, сканируется один раз
    got2, _ = data_mask_sweep([str(tmp_path / 'card.json'), str(tmp_path)], ['deadbeefcafe1234'])
    assert len(got2['hits']) == 1 and got2['scanned'] == 2


def test_data_mask_values_file_or_list(tmp_path):
    f = tmp_path / 'values.txt'
    f.write_text('["one", "two"]\n', encoding='utf-8')
    assert data_mask_values(str(f)) == (['one', 'two'], None)
    f.write_text('one\ntwo\n\n', encoding='utf-8')
    assert data_mask_values(str(f)) == (['one', 'two'], None)
    assert data_mask_values([])[1] is not None

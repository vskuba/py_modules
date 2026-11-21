"""Тесты маски секретов в JSON-дампах."""
from data_.data_mask import data_mask_json


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

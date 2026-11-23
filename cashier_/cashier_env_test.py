"""Тесты карты окружений Cashier: адреса, базы и заголовки для периметра."""
from cashier_.cashier_env import (CASHIER_ENV_DATABASES, cashier_env_database, cashier_env_explain,
                                  cashier_env_headers, cashier_env_host, cashier_env_map)


def test_cashier_env_host_stage_and_prod_differ_by_prefix():
    """Среда разворачивается префиксом: stage — `stage-`, prod — без префикса вовсе."""
    assert cashier_env_host('stage', 'api') == 'https://api.stage-ipaygateway.com'
    assert cashier_env_host('prod', 'api') == 'https://api.ipaygateway.com'
    assert cashier_env_host('dev', 'dashboard') == 'https://dev-ipaygateway.com'
    assert cashier_env_host('prod', 'dashboard') == 'https://ipaygateway.com'


def test_cashier_env_host_non_http_role_has_no_scheme():
    """База — не HTTP-роль: схемы к ней не приклеивают, иначе `mysql` не подключится."""
    assert cashier_env_host('stage', 'db') == 'db.stage-ipaygateway.com'
    assert cashier_env_host('stage', 'db', scheme=False) == 'db.stage-ipaygateway.com'


def test_cashier_env_host_unknown_env_named():
    """Опечатка в среде называется сразу: запрос не уходит в выдуманный домен."""
    try:
        cashier_env_host('qa', 'api')
    except ValueError as exc:
        assert 'qa' in str(exc)
    else:
        raise AssertionError('на среде `qa` ожидалась ошибка, а адрес отдали')


def test_cashier_env_host_unknown_role_named():
    """И роли, которой нет в карте, — с перечислением тех, что бывают."""
    try:
        cashier_env_host('stage', 'kafka')
    except ValueError as exc:
        assert 'kafka' in str(exc) and 'api' in str(exc)
    else:
        raise AssertionError('на роли `kafka` ожидалась ошибка, а адрес отдали')


def test_cashier_env_database_names_are_dashed():
    """Имена баз — с дефисами, как они лежат на сервере; `aggregate` и `aggregate-psp` — одно."""
    assert cashier_env_database('apm') == 'cashier-apm'
    assert cashier_env_database('aggregate') == cashier_env_database('aggregate-psp')
    assert set(CASHIER_ENV_DATABASES.values()) >= {'cashier-accounts', 'cashier-withdrawal'}


def test_cashier_env_explain_reads_cloudflare_1010():
    """Пустой 403 с `1010` объясняется как «не браузер», а не как отказ приложения."""
    text = cashier_env_explain(403, 'error code: 1010', 'stage-ipaygateway.com')
    assert 'Cloudflare' in text and '1010' in text and 'cashier_env_headers' in text
    other = cashier_env_explain(500, 'Internal error', 'api.stage-ipaygateway.com')
    assert 'Cloudflare' not in other and 'HTTP 500' in other


def test_cashier_env_headers_does_not_look_like_python():
    """Заголовки похожи на браузер: с User-Agent и Accepted типом, без `Python-urllib`."""
    head = cashier_env_headers({'Authorization': 'Bearer x'})
    assert 'Python' not in head.get('User-Agent', '')
    assert 'application/json' in head.get('Accept', '')
    assert head['Authorization'] == 'Bearer x'


def test_cashier_env_map_shows_whole_contour():
    """Карта среды собрана целиком: роли и базы одного контура, прод — без `stage`."""
    contour = cashier_env_map('prod')
    assert contour['hosts']['api'] == 'https://api.ipaygateway.com'
    assert 'cashier-withdrawal' in contour['databases']
    assert not any('stage' in str(value) for value in contour['hosts'].values())

"""Тесты провайдера: базы, фильтр по бренду и признаки из колонок-флагов."""
import pytest
from cashier_.cashier_provider import (_cashier_provider_brand_cut, _cashier_provider_brand_params,
                                       _cashier_provider_databases, _cashier_provider_flags,
                                       cashier_provider_webhook)

UUID = '9cc67582-9be0-475a-b7f3-2637193806ee'


def test_cashier_provider_databases_are_service_databases():
    """Базы сервисов не путаются и не дублируются: `aggregate` и `aggregate-psp` — одна."""
    names = _cashier_provider_databases()
    assert names == ['cashier-aggregate-psp', 'cashier-apm', 'cashier-withdrawal']
    assert _cashier_provider_databases(('aggregate', 'aggregate-psp')) == ['cashier-aggregate-psp']


def test_cashier_provider_brand_placeholders_match_params():
    """Каждому `%s` в фильтре — свой параметр: иначе запрос падает на подстановке."""
    for brand in ('WC1', UUID, ''):
        assert _cashier_provider_brand_cut(brand).count('%s') == len(_cashier_provider_brand_params(brand))


def test_cashier_provider_brand_filter_takes_uuid_and_name():
    """Фильтр по бренду ищет и по uuid, и по имени: человек вводит одно из двух."""
    assert _cashier_provider_brand_params(UUID) == [UUID, UUID]
    assert _cashier_provider_brand_params(' WC1 ') == [' WC1 ', 'wc1']
    assert _cashier_provider_brand_cut('WC1').strip().startswith('AND')


def test_cashier_provider_flags_reads_json_of_every_shape():
    """JSON-колонка — строка, словарь или NULL: на выходе всегда словарь, без падения."""
    assert _cashier_provider_flags('{"is_crypto": 0}') == {'is_crypto': 0}
    assert _cashier_provider_flags({'external': 1}) == {'external': 1}
    assert _cashier_provider_flags(None) == {}
    assert _cashier_provider_flags('{что-то не то') == {'raw': '{что-то не то'}


def test_cashier_provider_webhook_is_per_provider_instance():
    """Webhook — путь с uuid экземпляра; без uuid остаётся префикс, который ждёт сервис."""
    assert cashier_provider_webhook('nuvei', UUID, 'stage') == (
        f'https://secure.stage-ipaygateway.com/notification/nuvei/{UUID}')
    assert cashier_provider_webhook('nuvei', env='prod') == 'https://secure.ipaygateway.com/notification/nuvei'


def test_cashier_provider_webhook_of_unknown_env_named():
    """Среды нет — она называется, а не склеивается в `https://secure.qa-ipaygateway.com`."""
    with pytest.raises(ValueError) as bad:
        cashier_provider_webhook('nuvei', UUID, 'qa')
    assert 'qa' in str(bad.value)

"""Тесты PHP-примет overengineering: правила на словарях разбора и один сквозной прогон через php."""
import shutil
from pathlib import Path

import pytest

from tool_.tool_overengineering_php import _fixed_param, _passthrough, _single_impl, tool_overengineering_php

CASHIER_VENDOR = Path('/var/www/cashier-service-payment-gateway/vendor')


def test_passthrough_skips_doors_factories_routes_and_protocol():
    """Обёртка — находка; дверь к private, `new X`, маршрут и метод Iterator — нет."""
    found = _passthrough(_cls(
        _method('getWallet', ['id'], forward={'target': '$this->apiManager->walletCreate', 'args': ['id']}),
        _method('pay', ['p'], forward={'target': '$this->doPay', 'args': ['p']}),
        _method('doPay', ['p'], private=True),
        _method('createSettingsManager', ['s'], forward={'target': 'new SettingsManager', 'args': ['s']}),
        _method('process', ['r'], forward={'target': '$this->processRedirect', 'args': ['r']}, routed=True),
        _method('current', [], forward={'target': '$this->getInnerIterator()->current', 'args': []}),
    ))
    assert [one['name'] for one in found] == ['Api::getWallet']


def test_fixed_param_private_only_literal_and_null_skipped():
    """Private-опция без пользователя и «всегда один литерал»; `null` и публичные — не судятся."""
    found = _fixed_param(_cls(
        _method('run', [], calls=[_call('gen', []), _call('gen', []),
                                  _call('send', [None, "'eu'"]), _call('send', [None, "'eu'"]),
                                  _call('opt', []), _call('pub', [])]),
        _method('gen', [('length', '12')], private=True),
        _method('send', [('x', None), ('region', "'us'")], private=True),
        _method('opt', [('cfg', 'null')], private=True),
        _method('pub', [('n', '1')]),
    ))
    assert [one['name'] for one in found] == ['Api::gen($length)', 'Api::send($region)']


def test_single_impl_counts_file_class_pairs_and_transitive(tmp_path):
    """Одноимённые реализации в разных файлах — разные; интерфейс над абстрактным с детьми — не находка."""
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    (tmp_path / 'a' / 'ApiManager.php').write_text('<?php class ApiManager extends AbstractApiManager {}')
    (tmp_path / 'b' / 'ApiManager.php').write_text('<?php class ApiManager extends AbstractApiManager {}')
    (tmp_path / 'Lonely.php').write_text('<?php class Only implements LonelyInterface {}')
    (tmp_path / 'Base.php').write_text('<?php abstract class AbstractApiManager implements ManagerInterface {}')

    def judge(name, kind):
        return _single_impl({'name': name, 'kind': kind, 'line': 1, 'methods': []}, tmp_path, {})

    assert judge('AbstractApiManager', 'abstract') == []
    assert judge('ManagerInterface', 'interface') == []
    assert [one['name'] for one in judge('LonelyInterface', 'interface')] == ['LonelyInterface']


@pytest.mark.skipif(not shutil.which('php') or not (CASHIER_VENDOR / 'nikic').is_dir(),
                    reason='нужны php и nikic/php-parser')
def test_end_to_end_through_php(tmp_path, monkeypatch):
    """Настоящий разбор: обёртка над чужим методом находится, PHP 8-синтаксис не мешает."""
    monkeypatch.setenv('TOOL_OVERENGINEERING_PHP_VENDOR', str(CASHIER_VENDOR))
    src = tmp_path / 'Manager.php'
    src.write_text('<?php\nfinal class Manager {\n'
                   '    public function __construct(private readonly Api $api) {}\n'
                   '    public function wallet(string $id): array { return $this->api->walletCreate($id); }\n'
                   '}\n')

    found, skipped = tool_overengineering_php([src], tmp_path)
    assert skipped == ''
    assert [(one['kind'], one['name']) for one in found] == [('passthrough', 'Manager::wallet')]


def _cls(*methods):
    return {'name': 'Api', 'kind': 'class', 'line': 1, 'methods': list(methods)}


def _method(name, params, private=False, forward=None, routed=False, calls=None):
    return {'name': name, 'line': 1, 'private': private, 'abstract': False, 'routed': routed,
            'params': [{'name': p, 'default': None, 'variadic': False} if isinstance(p, str)
                       else {'name': p[0], 'default': p[1], 'variadic': False} for p in params],
            'forward': forward, 'calls': calls or []}


def _call(name, values):
    return {'name': name, 'opaque': False, 'named': {},
            'pos': [{'given': True, 'value': v} for v in values]}

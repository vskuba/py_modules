"""Тесты процессора: имя папки, признаки по файлам и ветка checkout-а."""
from pathlib import Path

import pytest
from cashier_.cashier_processor import (_cashier_processor_branch, _cashier_processor_key,
                                        _cashier_processor_root, cashier_processor_matrix,
                                        cashier_processor_supports)


def test_cashier_processor_key_folds_spelling_of_folder():
    """`phantom-pay` из базы и `PhantomPay` из `processors/` — одно и то же: сравнение по буквам."""
    assert _cashier_processor_key('phantom-pay') == _cashier_processor_key('PhantomPay')
    assert _cashier_processor_key('aggregate-psp') == _cashier_processor_key('AggregatePsp')
    assert _cashier_processor_key('') == ''


def test_cashier_processor_root_without_processors_named(tmp_path: Path):
    """Checkout без `processors/` — ошибка с путём, а не пустая матрица «процессоров нет»."""
    with pytest.raises(RuntimeError) as bad:
        _cashier_processor_root(str(tmp_path))
    assert 'processors' in str(bad.value) or 'processor' in str(bad.value)


def test_cashier_processor_branch_reads_head_without_git(tmp_path: Path):
    """Ветка — из `.git/HEAD`, без процесса git: ответ матрицы зависит от ветки."""
    head = tmp_path / '.git'
    head.mkdir()
    (head / 'HEAD').write_text('ref: refs/heads/feature/nuvei-refund-via-payout\n', encoding='utf-8')
    assert _cashier_processor_branch(tmp_path) == 'nuvei-refund-via-payout'
    (head / 'HEAD').write_text('0123456789abcdef0123456789abcdef01234567\n', encoding='utf-8')
    assert _cashier_processor_branch(tmp_path) == '0123456789ab'
    assert _cashier_processor_branch(tmp_path / 'nowhere') == ''


def test_cashier_processor_supports_of_unknown_capability_named():
    """Опечатка в возможности называется списком: «нет» по вине ключа и «нет» у процессора — разные вещи."""
    with pytest.raises(ValueError) as bad:
        cashier_processor_supports('Nuvei', 'refound')
    assert 'refund' in str(bad.value)


def test_cashier_processor_matrix_reads_capabilities(tmp_path: Path):
    """Матрица собрана по файлам: что есть в папке — то и умеет, лишнего не придумывает."""
    root = _fake_checkout(tmp_path)
    rows = cashier_processor_matrix('fake', checkout=str(root))
    assert [row['processor'] for row in rows] == ['FakePay']
    row = rows[0]
    assert row['card'] and row['refund'] and row['payout']
    assert not row['apm'] and not row['crypto']
    assert row['branch'] == 'main'
    assert row['files']['refund'] == ['FakePay/RefundPaymentProcessor.php',
                                      'FakePay/Factory/RefundProcessorFactory.php']


def test_cashier_processor_matrix_skips_other_processors(tmp_path: Path):
    """Фильтр по подстроке не тянет соседей: `fake` — только FakePay."""
    root = _fake_checkout(tmp_path)
    (root / 'processors' / 'Other').mkdir()
    assert [row['processor'] for row in cashier_processor_matrix('fake', checkout=str(root))] == ['FakePay']
    assert {row['processor'] for row in cashier_processor_matrix(checkout=str(root))} == {'FakePay', 'Other'}


def test_cashier_processor_supports_of_missing_folder_is_no(tmp_path: Path):
    """Папки нет — «нет», а не падение: так же отвечает проверка процессора из другой ветки."""
    root = _fake_checkout(tmp_path)
    assert cashier_processor_supports('fakepay', 'refund', checkout=str(root))
    assert cashier_processor_supports('UnitFinance', 'refund', checkout=str(root)) is False


def _fake_checkout(tmp_path: Path) -> Path:
    """Checkout с одним процессором: карта, возврат и выплата, без APM."""
    folder = tmp_path / 'processors' / 'FakePay'
    (folder / 'Factory').mkdir(parents=True)
    for name in ('CardPaymentProcessor.php', 'RefundPaymentProcessor.php',
                 'PayoutPaymentProcessor.php', 'Factory/RefundProcessorFactory.php'):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<?php\n', encoding='utf-8')
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
    return tmp_path

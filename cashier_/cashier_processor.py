"""
Процессор payment-gateway: что он умеет в коде и зарегистрирован ли в базе.

`processors/<Имя>/` в `cashier-service-payment-gateway` — 99 папок интеграций, и вопрос
«а этот провайдер вообще умеет возвраты/выплаты?» решается не догадками, а наличием
класса-обработчика: `RefundPaymentProcessor.php` + `Factory/RefundProcessorFactory.php`.
Резолвер (`src/Utils/Processing/Resolver/RefundPaymentProcessorFactoryResolver.php`) без
фабрики кидает `ProcessorNotFoundException: Refund payment processor factory not found` —
то есть «refund не поддерживается» это не мнение, а свойство дерева файлов этой ветки.

Второй вопрос, который всегда идёт следом: код есть, а в базе процессор не зарегистрирован
(миграции `payment_provider_info` нет) — в дашборде его не выбрать. `cashier_processor_gaps`
сводит два факта в один ответ; регистрацию читает у `cashier_provider`, а не своим SQL.

    python -m cashier_.cashier_processor matrix --processor nuvei
    python -m cashier_.cashier_processor supports nuvei refund
    python -m cashier_.cashier_processor gaps nuvei --env stage

⚠ Матрица — по **рабочему checkout**: возможности зависят от ветки (у Nuvei refund-классы
появились только в `feature/nuvei-refund-via-payout`), поэтому в ответе лежит и путь, и
ветка. Ничего не меняем: только чтение файловой системы и базы.
"""
import argparse
import re
from pathlib import Path

from cashier_.cashier_env import cashier_env_root
from cashier_.cashier_provider import CASHIER_PROVIDER_SERVICES, cashier_provider_info

# Где лежит checkout payment-gateway с папкой процессоров — по умолчанию рабочий проект.
CASHIER_PROCESSOR_CHECKOUT = '/var/www/cashier-service-payment-gateway'

# Папка процессоров внутри checkout.
CASHIER_PROCESSOR_DIR = 'processors'

# Что умеет процессор — по каким файлам это видно. Порядок здесь = порядок колонок матрицы.
CASHIER_PROCESSOR_CAPABILITIES = {
    'card': ('CardPaymentProcessor.php', 'Factory/CardProcessorFactory.php'),
    'apm': ('ApmPaymentProcessor.php', 'Factory/ApmProcessorFactory.php',
            'Factory/ApmPaymentProcessorFactory.php'),
    'payout': ('PayoutPaymentProcessor.php', 'Factory/PayoutProcessorFactory.php'),
    'refund': ('RefundPaymentProcessor.php', 'Factory/RefundProcessorFactory.php'),
    'crypto': ('CryptoPaymentProcessor.php', 'CryptoPaymentCallbackProcessor.php'),
    'deposit_page': ('DepositPageProcessor.php', 'DepositPageReactRequestProcessor.php'),
    'check_status': ('PaymentStatusProvider.php', 'Factory/CheckStatusProcessorFactory.php'),
    'notification': ('PaymentNotificationProcessor.php', 'Factory/NotificationProcessorFactory.php'),
    'redirect': ('PaymentRedirectProcessor.php', 'RedirectProcessor.php',
                 'Factory/RedirectProcessorFactory.php'),
    'authorization': ('PaymentAuthorizationProcessor.php', 'Factory/AuthorizationProcessorFactory.php'),
}


def cashier_processor_matrix(processor: str = '', checkout: str = '') -> list:
    """
    Матрица возможностей процессоров по файлам этой ветки — кто что умеет в коде.

    Args:
        processor: подстрока имени (`nuvei`), пусто — все 99 папок.
        checkout: какой checkout смотреть; по умолчанию `CASHIER_PROCESSOR_CHECKOUT`.

    Returns:
        `[{'processor', 'branch', ... возможности, 'files': {возможность: путь}}, …]`,
        отсортировано по имени; `branch` — ветка checkout-а, потому что ответ от неё зависит.
    """
    root = _cashier_processor_root(checkout)
    rows = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if processor.strip().lower() and processor.strip().lower() not in folder.name.lower():
            continue
        files = {capability: _cashier_processor_files(folder, capability)
                 for capability in CASHIER_PROCESSOR_CAPABILITIES}
        rows.append(dict({'processor': folder.name, 'branch': _cashier_processor_branch(root.parent)},
                         **{capability: bool(found) for capability, found in files.items()},
                         files={k: v for k, v in files.items() if v}))
    return rows


def cashier_processor_supports(processor: str, capability: str, checkout: str = '') -> bool:
    """
    Умеет ли процессор возможность в этой ветке — True/False, без разбора причин.

    `capability` — ключ из `CASHIER_PROCESSOR_CAPABILITIES` (`card`, `apm`, `payout`,
    `refund`). False означает и «классов нет», и «папки такой нет»: проверивший видит это
    по `cashier_processor_matrix`.

    Raises:
        ValueError: возможности с таким именем нет (опечатка в ключе).
    """
    if capability not in CASHIER_PROCESSOR_CAPABILITIES:
        raise ValueError(f'возможности {capability!r} нет: бывает '
                         f'{", ".join(CASHIER_PROCESSOR_CAPABILITIES)}')
    folder = _cashier_processor_root(checkout) / _cashier_processor_folder(processor, checkout)
    if not folder.is_dir():
        return False
    return bool(_cashier_processor_files(folder, capability))


def cashier_processor_gaps(processor: str, env: str = 'stage', root: str = '',
                           checkout: str = '') -> dict:
    """
    Где разрыв: процессор есть в коде, но не зарегистрирован в базе (или наоборот).

    Базы сервисов (`CASHIER_PROVIDER_SERVICES`) сравниваются с папкой в `processors/`:
    тип без строки `payment_provider_info` в дашборде не появляется, а строка без кода
    падает на первом же платеже. Ответ читается как «куда идти чинить».

    Returns:
        `{'processor', 'code', 'branch', 'capabilities', 'registered', 'missing', 'orphans'}`;
        `registered`/`missing` — базы сервисов, `orphans` — базы, где процессор зарегистрирован,
        а папки в коде нет.
    """
    folder = _cashier_processor_folder(processor, checkout)
    rows = cashier_provider_info(processor, env=env, root=root)
    registered = sorted({row['service'].removeprefix('cashier-') for row in rows})
    capabilities = []
    if folder:
        here = _cashier_processor_root(checkout) / folder
        capabilities = sorted(capability for capability in CASHIER_PROCESSOR_CAPABILITIES
                              if _cashier_processor_files(here, capability))
    return {'processor': processor, 'folder': folder, 'code': bool(folder),
            'branch': _cashier_processor_branch(_cashier_processor_root(checkout).parent),
            'capabilities': capabilities,
            'registered': registered,
            'missing': sorted(set(CASHIER_PROVIDER_SERVICES) - set(registered)),
            'orphans': sorted(set(registered) - set(CASHIER_PROVIDER_SERVICES))}


# ------------------------------------------------------------------- приватные функции

def _cashier_processor_root(checkout: str = '') -> Path:
    """Папка `processors/` выбранного checkout-а; если её нет — внятная ошибка."""
    base = Path(cashier_env_root(checkout))
    root = base / CASHIER_PROCESSOR_DIR
    if not root.is_dir():
        raise RuntimeError(f'папки {root} нет: это не checkout payment-gateway '
                           f'(или процессоры лежат в другом месте — укажи --root)')
    return root


def _cashier_processor_files(folder: Path, capability: str) -> list:
    """Найденные файлы возможности — относительные пути внутри папки процессора."""
    found = []
    for name in CASHIER_PROCESSOR_CAPABILITIES[capability]:
        path = folder / name
        if path.is_file():
            found.append(str(path.relative_to(folder.parent)))
    return found


def _cashier_processor_folder(processor: str, checkout: str = '') -> str:
    """Имя из базы (`phantom-pay`) — к папке (`PhantomPay`); совпадение по буквам и цифрам."""
    wanted = _cashier_processor_key(processor)
    if not wanted:
        return ''
    try:
        root = _cashier_processor_root(checkout)
    except RuntimeError:
        return ''
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if _cashier_processor_key(folder.name) == wanted:
            return folder.name
    return ''


def _cashier_processor_key(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _cashier_processor_branch(checkout: Path) -> str:
    """Текущая ветка checkout-а из `.git/HEAD` — без git и без вызова процесса."""
    head = checkout / '.git' / 'HEAD'
    try:
        text = head.read_text().strip()
    except OSError:
        return ''
    return text.rsplit('/', 1)[-1] if text.startswith('ref:') else text[:12]


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description='процессор payment-gateway: возможности и регистрация')
    _P.add_argument('command', nargs='?', default='matrix', choices=('matrix', 'supports', 'gaps'))
    _P.add_argument('processor', nargs='?', default='', help='имя процессора (nuvei, phantom-pay, …)')
    _P.add_argument('capability', nargs='?', default='', help='возможность (для supports)')
    _P.add_argument('--env', default='stage', help='среда, где смотреть регистрацию')
    _P.add_argument('--root', default='', help='checkout, чьи креды БД читать')
    _P.add_argument('--checkout', default='', help='checkout payment-gateway с папкой processors')
    _C = _P.parse_args()
    try:
        if _C.command == 'supports':
            print(f"{_C.processor} {_C.capability}: "
                  f"{'да' if cashier_processor_supports(_C.processor, _C.capability, _C.checkout) else 'нет'}")
        elif _C.command == 'gaps':
            for line, value in cashier_processor_gaps(_C.processor, env=_C.env, root=_C.root,
                                                      checkout=_C.checkout).items():
                print(f'{line}: {", ".join(map(str, value)) if isinstance(value, list) else value}')
        else:
            rows = cashier_processor_matrix(_C.processor, _C.checkout)
            if not rows:
                raise SystemExit(f'процессора {(_C.processor or "?")!r} в папке processors нет')
            names = [c for c in CASHIER_PROCESSOR_CAPABILITIES
                     if any(row[c] for row in rows)]
            print('processor'.ljust(22) + ''.join(name[:6].ljust(7) for name in names) + 'ветка')
            for row in rows:
                print(row['processor'].ljust(22)
                      + ''.join(('+' if row[name] else '·').ljust(7) for name in names)
                      + row['branch'])
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f'ошибка: {exc}')

"""
Карта окружений Cashier — один файл знает, где что живёт.

Адреса экосистемы приходится помнить всем, кто ходит в Cashier извне: дашборд, API
брендов, вебхуки провайдеров, базы, консоль брокера. Когда их держат в голове, ошибки
тихие и дорогие: `api.stage.ipaygateway.com` вместо `api.stage-ipaygateway.com`, точка
вместо дефиса, забывшийся префикс `stage-` — и запрос уходит в несуществующий хост либо,
что хуже, в прод. То же с базами: у каждого сервиса своя база с дефисами в имени, а не
«база сервиса».

Здесь один эксперт на всю экосистему: роль («дашборд», «API», «вебхук», «база») и среда
возвращают адрес, имя базы и каталог, из которого читать ключи доступа. Формула хоста
одна на все роли: `<роль>.<среда>ipaygateway.com`, у prod среда — пустая строка; у
дашборда роли нет вовсе, а у прод-дашборда нет и префикса.
"""
import json
from pathlib import Path

# Среды, для которых у Cashier есть отдельный контур. Порядок — от новой к живой.
CASHIER_ENV_NAMES = ('dev', 'stage', 'prod')

# Хост роли: `{env}` разворачивается в `dev-`, `stage-` или пустую строку (prod).
# Значения без `{env}` — исключений из формулы: прод-дашборд живёт на голом домене.
CASHIER_ENV_HOSTS = {
    'dashboard': '{env}ipaygateway.com',
    'api': 'api.{env}ipaygateway.com',
    'secure': 'secure.{env}ipaygateway.com',
    'deposit_page': 'deposit.{env}ipaygateway.com',
    'aggregate': 'aggregate-psp.{env}ipaygateway.com',
    'apm': 'apm.{env}ipaygateway.com',
    'withdrawal': 'withdrawal.{env}ipaygateway.com',
    'accounts': 'accounts.{env}ipaygateway.com',
    'db': 'db.{env}ipaygateway.com',
    'rabbit': 'rabbitmq.{env}ipaygateway.com',
}

# Роли, к которым ходят по HTTP: к ним добавляется схема, остальное — голые хосты.
CASHIER_ENV_HTTP_ROLES = ('dashboard', 'api', 'secure', 'deposit_page', 'aggregate',
                          'apm', 'withdrawal', 'accounts', 'rabbit')

# Сервис → имя его базы на сервере окружения (базы на сервере лежат рядом, с дефисами).
CASHIER_ENV_DATABASES = {
    'accounts': 'cashier-accounts',
    'aggregate': 'cashier-aggregate-psp',
    'aggregate-psp': 'cashier-aggregate-psp',
    'apm': 'cashier-apm',
    'withdrawal': 'cashier-withdrawal',
    'deposit': 'cashier-deposit',
    'payment-gateway': 'cashier-payment-gateway',
    'report': 'cashier-report',
    'notification': 'cashier-notification',
}

# Где лежат ключи `MYSQL_<среда>_*` и `CASHIER_UI_<среда>_*`, когда текущий каталог
# своих `.env` не дал: это checkout, который держит настройки окружений целиком.
CASHIER_ENV_DEFAULT_ROOT = '/var/www/cashier-service-payment-gateway'


def cashier_env_host(env: str = 'stage', role: str = 'dashboard', scheme: bool = True) -> str:
    """
    Адрес роли в среде — чтобы не вспоминать формулу домена на каждый вызов.

    Args:
        env: `dev`, `stage` или `prod`.
        role: `dashboard`, `api`, `secure` (вебхуки провайдеров), `deposit_page`,
            `aggregate`, `apm`, `withdrawal`, `accounts`, `db`, `rabbit`.
        scheme: добавить `https://`; `False` — голый хост (для `db` схема не нужна).

    Raises:
        ValueError: среды или роли нет в карте.
    """
    key = (env or 'stage').lower()
    if key not in CASHIER_ENV_NAMES:
        raise ValueError(f'среды {env!r} нет: бывает {", ".join(CASHIER_ENV_NAMES)}')
    template = CASHIER_ENV_HOSTS.get(role)
    if template is None:
        raise ValueError(f'роли {role!r} нет: бывает {", ".join(sorted(CASHIER_ENV_HOSTS))}')
    host = template.format(env='' if key == 'prod' else f'{key}-')
    if not scheme or role not in CASHIER_ENV_HTTP_ROLES:
        return host
    return f'https://{host}'


def cashier_env_database(service: str) -> str:
    """
    Имя базы сервиса на сервере окружения; принимает и сервис, и уже готовое имя базы.

    Raises:
        ValueError: сервиса нет в списке.
    """
    key = (service or '').lower().removeprefix('cashier-')
    if key not in CASHIER_ENV_DATABASES:
        raise ValueError(f'сервиса {service!r} нет: бывает {", ".join(sorted(set(CASHIER_ENV_DATABASES)))}')
    return CASHIER_ENV_DATABASES[key]


def cashier_env_root(root: str = '') -> str:
    """
    Откуда читать ключи окружения (`MYSQL_STAGE_*`, `CASHIER_UI_STAGE_*`).

    Указанный `root` — как есть. Иначе: текущий каталог, если в нём есть `.env` или
    `.env.local`, а нет — checkout из `CASHIER_ENV_DEFAULT_ROOT`. Пустой ответ —
    «читай из окружения процесса»: так работают ключи, пришедшие из оболочки.
    """
    if root:
        return root
    cwd = Path.cwd()
    if (cwd / '.env.local').is_file() or (cwd / '.env').is_file():
        return str(cwd)
    if Path(CASHIER_ENV_DEFAULT_ROOT, '.env.local').is_file():
        return CASHIER_ENV_DEFAULT_ROOT
    return ''


def cashier_env_map(env: str = 'stage') -> dict:
    """Вся карта среды одним словарём — проверить адрес, не открывая файл."""
    return {
        'env': (env or 'stage').lower(),
        'hosts': {role: cashier_env_host(env, role) for role in CASHIER_ENV_HOSTS},
        'databases': sorted(set(CASHIER_ENV_DATABASES.values())),
    }


def cashier_env_headers(extra: dict | None = None) -> dict:
    """
    Заголовки, которые пропускает периметр Cashier: Cloudflare отсекает клиентов не-браузеров.

    Дефолтный `Python-urllib/3.x` на dev- и stage-хостах получает 403 с `error code: 1010`
    ещё до приложения; по этому признаку ошибка выглядит как «сервер не хочет меня пускать»,
    хотя дело только в строке клиента. Берём согласованный десктопный набор (`useragent_make`).

    Args:
        extra: свои заголовки поверх (Cookie, Authorization).
    """
    from useragent_.useragent_make import useragent_make
    headers = {**useragent_make('chrome-win')['headers'],
               'Accept': 'application/json', 'Content-Type': 'application/json'}
    return {**headers, **(extra or {})}


def cashier_env_explain(status: int, text: str, host: str = '') -> str:
    """
    Почему периметр ответил не-JSON: пустой 403 с `1010` — это Cloudflare, а не приложение.

    Без этой строки 403 читается как «сервер отверг запрос», и идут править запрос,
    тогда как тело ответа говорит «ты не браузер».
    """
    if status == 403 and '1010' in text:
        return (f'{host or "хост"} отрезан Cloudflare (код 1010): клиент не похож на браузер, '
                f'заголовки нужно брать из cashier_env_headers')
    return f'{host or "хост"} ответил HTTP {status}: {text[:200]}'


if __name__ == '__main__':
    import argparse
    _P = argparse.ArgumentParser(description='карта окружений Cashier')
    _P.add_argument('env', nargs='?', default='stage')
    _C = _P.parse_args()
    print(json.dumps(cashier_env_map(_C.env), ensure_ascii=False, indent=2))

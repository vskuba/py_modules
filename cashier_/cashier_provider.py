"""
Провайдер Cashier: где он зарегистрирован, у каких брендов стоит и чем разрешён.

«Провайдера нет в дашборде» — это всегда один из трёх разных ответов, и каждый руками
стоит по запросу на сервис: тип процессора (`payment_provider_info`) может быть не
зарегистрирован нигде (миграция не доехала), может быть зарегистрирован, но не иметь ни
одного экземпляра под брендом (`payment_provider` пуст — в UI его не видно), и может
стоять под брендом, но быть выключенным. Плюс возврат: он живёт в `cashier-withdrawal`
отдельной строкой `withdrawal_type='REFUND'`, и её отсутствие не видно ни в одном списке
PSP/APM — при этом refund API отвечает той самой ошибкой про настройку провайдера.

Файл отвечает на свои вопросы одним запросом на вызов: базы сервисов лежат на одном
сервере, поэтому `UNION ALL` и подзапросы через `` `cashier-apm`.… `` работают сразу.

    python -m cashier_.cashier_provider info nuvei --env stage     # где тип зарегистрирован
    python -m cashier_.cashier_provider brands nuvei --env stage   # у каких брендов экземпляр
    python -m cashier_.cashier_provider refunds --env stage --brand WC1   # чем разрешён возврат
    python -m cashier_.cashier_provider webhook nuvei <uuid> --env stage  # адрес для back-office

⚠ Правил тут не выдумывают: `is_refund_supported` — колонка-наследие (в коде её не читают),
воротами возврата служат строки `payment_provider` с `withdrawal_type='REFUND'`.
⚠ Только чтение: `mysql_query` без `--write`, prod — по прямой просьбе Хозяина.
"""
import argparse
import json

from cashier_.cashier_brand import _cashier_brand_looks_uuid
from cashier_.cashier_env import cashier_env_database, cashier_env_host, cashier_env_root
from mysql_.mysql_query import mysql_query_run

# Сервисы, где у провайдера есть свой реестр типов и свои экземпляры под брендами.
CASHIER_PROVIDER_SERVICES = ('aggregate-psp', 'apm', 'withdrawal')

# Что показывать про тип процессора в каждом сервисе: колонки-признаки своих таблиц.
CASHIER_PROVIDER_FLAGS = {
    'cashier-aggregate-psp': ('is_refund_supported', 'external'),
    'cashier-apm': ('is_crypto', 'payment_method', 'public_title'),
    'cashier-withdrawal': ('is_crypto', 'external'),
}

# Тип вывода, которым является возврат: та же таблица `payment_provider`, другая ветка.
CASHIER_PROVIDER_REFUND_TYPE = 'REFUND'

# Где искать депозитный провайдер, на который смотрит строка возврата.
CASHIER_PROVIDER_DEPOSIT_DATABASES = ('cashier-aggregate-psp', 'cashier-apm')


def cashier_provider_info(processor: str = '', env: str = 'stage', root: str = '',
                          limit: int = 60) -> list:
    """
    Где тип процессора зарегистрирован — по всем сервисам разом, с числом экземпляров.

    Без `processor` перечисляет все типы (их десятки), с `processor` — фильтрует по
    подстроке (`nuvei`, `phantom`). Строки: `{'service', 'uuid', 'title', 'processor_name',
    'instances', 'active', 'flags'}`; `instances` — сколько брендов завело провайдера,
    `active` — сколько из них включены. Сервиса нет в ответе = типа там нет: в дашборде
    его не выбрать, нужна миграция `payment_provider_info`.
    """
    like = f'%{(processor or "").strip().lower()}%'
    branches, params = [], []
    for database in _cashier_provider_databases():
        flags = ', '.join(f"'{flag}', ppi.{flag}" for flag in CASHIER_PROVIDER_FLAGS[database])
        branches.append(
            f"SELECT '{database}' AS service, ppi.uuid, ppi.title, ppi.processor_name, "
            f"(SELECT COUNT(*) FROM `{database}`.payment_provider i "
            f' WHERE i.payment_provider_info_id = ppi.id) AS instances, '
            f"(SELECT COUNT(*) FROM `{database}`.payment_provider i "
            f" WHERE i.payment_provider_info_id = ppi.id AND i.state = 'ENABLED') AS active, "
            f'JSON_OBJECT({flags}) AS flags '
            f'FROM `{database}`.payment_provider_info ppi WHERE LOWER(ppi.processor_name) LIKE %s')
        params.append(like)
    rows = _cashier_provider_rows(env, root, ' UNION ALL '.join(branches) +
                                  f' ORDER BY processor_name, service LIMIT {int(limit)}',
                                  tuple(params))
    return [dict(row, flags=_cashier_provider_flags(row.get('flags'))) for row in rows]


def cashier_provider_brands(processor: str, env: str = 'stage', root: str = '', brand: str = '',
                            limit: int = 200) -> list:
    """
    У каких брендов стоит экземпляр процессора и в каком состоянии — список зоны риска.

    Нужен перед правкой провайдера (settings, каскад): трогая строку, знаешь, кого она
    касается. `brand` — имя или uuid бренда, по нему и режется. Строки:
    `{'service', 'brand', 'brand_uuid', 'alias', 'state', 'provider_uuid'}`.
    """
    branches, params = [], []
    for database in _cashier_provider_databases():
        branches.append(
            f"SELECT '{database}' AS service, a.name AS brand, a.uuid AS brand_uuid, "
            f'pp.alias, pp.state, pp.uuid AS provider_uuid '
            f"FROM `{database}`.payment_provider pp "
            f"JOIN `{database}`.payment_provider_info ppi ON ppi.id = pp.payment_provider_info_id "
            f"JOIN `{database}`.account a ON a.id = pp.account_id "
            f'WHERE LOWER(ppi.processor_name) = %s' + _cashier_provider_brand_cut(brand))
        params += [processor.strip().lower()] + _cashier_provider_brand_params(brand)
    return _cashier_provider_rows(env, root, ' UNION ALL '.join(branches) +
                                  f' ORDER BY brand, service LIMIT {int(limit)}', tuple(params))


def cashier_provider_refunds(processor: str = '', env: str = 'stage', root: str = '',
                             brand: str = '', limit: int = 200) -> list:
    """
    Чем разрешён возврат: строки `withdrawal_type='REFUND'` и на какой провайдер они смотрят.

    Возврат — не отдельная сущность, а запись в `cashier-withdrawal.payment_provider`,
    привязанная `deposit_provider_uuid` к экземпляру депозитного провайдера. Нет её —
    refund API отвергает запрос, сколько бы ни был настроен PSP. `is_refund_supported`
    в `cashier-aggregate-psp` на это не влияет: в коде её не читают.
    Строки: `{'brand', 'brand_uuid', 'alias', 'state', 'provider_type', 'refund_uuid',
    'processor_name', 'deposit_provider': {service, alias, state} | None}`; `None` —
    депозитного экземпляра уже нет, ссылка висит.
    """
    cut = f"WHERE pp.withdrawal_type = '{CASHIER_PROVIDER_REFUND_TYPE}'"
    params = []
    if (processor or '').strip():
        cut += ' AND LOWER(ppi.processor_name) = %s'
        params.append(processor.strip().lower())
    cut += _cashier_provider_brand_cut(brand)
    params += _cashier_provider_brand_params(brand)
    # Депозитный провайдер добирается тем же запросом: он в чужой базе того же сервера,
    # и без него строка «REFUND на PSP» ни к какому клиреру не привязывается.
    lookups = ', '.join(
        f"(SELECT JSON_OBJECT('service', '{database}', 'alias', i.alias, 'state', i.state) "
        f"FROM `{database}`.payment_provider i WHERE i.uuid = pp.deposit_provider_uuid LIMIT 1)"
        for database in CASHIER_PROVIDER_DEPOSIT_DATABASES)
    rows = _cashier_provider_rows(env, root,
                                  f'SELECT a.name AS brand, a.uuid AS brand_uuid, pp.alias, pp.state, '
                                  f'pp.provider_type, pp.uuid AS refund_uuid, pp.deposit_provider_uuid, '
                                  f'ppi.processor_name, '
                                  f'COALESCE({lookups}) AS deposit_provider '
                                  f'FROM `cashier-withdrawal`.payment_provider pp '
                                  f'JOIN `cashier-withdrawal`.payment_provider_info ppi '
                                  f'ON ppi.id = pp.payment_provider_info_id '
                                  f'JOIN `cashier-withdrawal`.account a ON a.id = pp.account_id '
                                  f'{cut} ORDER BY brand, processor_name LIMIT {int(limit)}',
                                  tuple(params))
    return [dict(row, deposit_provider=_cashier_provider_flags(row.get('deposit_provider')) or None)
            for row in rows]


def cashier_provider_webhook(processor: str, provider_uuid: str = '', env: str = 'stage') -> str:
    """
    Адрес webhook-а провайдера — таким его прописывают в back-office провайдера.

    `https://secure.<среда>ipaygateway.com/notification/<processor>/<uuid>`; без uuid —
    префикс, по нему видно, какой путь ждёт сервис. Живость проверяют пустым POST:
    живой путь отвечает `200 OK`.
    """
    tail = f"/{provider_uuid.strip('/')}" if provider_uuid else ''
    return f"{cashier_env_host(env, 'secure')}/notification/{(processor or '').strip('/')}{tail}"


# ------------------------------------------------------------------- приватные функции

def _cashier_provider_databases(services: tuple = ()) -> list:
    names = []
    for service in (services or CASHIER_PROVIDER_SERVICES):
        database = cashier_env_database(service)
        if database not in names:
            names.append(database)
    return names


def _cashier_provider_brand_cut(brand: str) -> str:
    return '' if not brand else ' AND (a.uuid = %s OR LOWER(a.name) = %s)'


def _cashier_provider_brand_params(brand: str) -> list:
    if not brand:
        return []
    return [brand, brand if _cashier_brand_looks_uuid(brand) else brand.strip().lower()]


def _cashier_provider_rows(env: str, root: str, sql: str, params: tuple) -> list:
    """
    Один читающий запрос к серверу среды; база — `information_schema`, таблицы с именем базы.

    Так запрос остаётся одним (один туннель), а отсутствующая на среде база видна по своему
    имени в ошибке MySQL, а не по пустому ответу.
    """
    return mysql_query_run(sql, params, root=cashier_env_root(root),
                           profile=(env or 'stage').lower(), database='information_schema')['rows']


def _cashier_provider_flags(raw) -> dict:
    """JSON из колонки — словарём; MySQL отдаёт его строкой, а NULL — пустотой."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {'raw': raw[:200]}
    return raw or {}


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description='провайдер Cashier: регистрация, бренды, возврат')
    _P.add_argument('command', nargs='?', default='info',
                    choices=('info', 'brands', 'refunds', 'webhook'), help='что посмотреть')
    _P.add_argument('processor', nargs='?', default='', help='имя процессора; для webhook — ещё и uuid')
    _P.add_argument('uuid', nargs='?', default='', help='uuid экземпляра (webhook)')
    _P.add_argument('--env', default='stage')
    _P.add_argument('--root', default='', help='checkout, чей .env.local читать')
    _P.add_argument('--brand', default='', help='имя или uuid бренда')
    _P.add_argument('--limit', type=int, default=0, help='обрезать список')
    _C = _P.parse_args()
    try:
        if _C.command == 'webhook':
            print(cashier_provider_webhook(_C.processor, _C.uuid, env=_C.env))
        elif _C.command == 'brands':
            for row in cashier_provider_brands(_C.processor, env=_C.env, root=_C.root,
                                               brand=_C.brand, limit=_C.limit or 200):
                print(f"{row['brand']:<20} {row['service']:<24} {row['state']:<8} {row['alias']}")
        elif _C.command == 'refunds':
            for row in cashier_provider_refunds(_C.processor, env=_C.env, root=_C.root,
                                                brand=_C.brand, limit=_C.limit or 200):
                deposit = row['deposit_provider'] or {}
                print(f"{row['brand']:<20} {row['processor_name']:<16} {row['provider_type']:<5} "
                      f"{row['state']:<8} депозит: {deposit.get('alias', '?')} "
                      f"({deposit.get('service', 'нет экземпляра')})")
        else:
            for row in cashier_provider_info(_C.processor, env=_C.env, root=_C.root,
                                             limit=_C.limit or 60):
                print(f"{row['processor_name']:<24} {row['service']:<24} "
                      f"экземпляров {row['instances']:>4}, включено {row['active']:>4}  {row['title']}")
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')

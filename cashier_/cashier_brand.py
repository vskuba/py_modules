"""
Бренд Cashier (= account) как точка входа: uuid, id по чужим базам, креды API и OAuth-токен.

«Взять бренд» в Cashier — это четыре разных вопроса, и каждый руками стоит отдельного
запроса: uuid (`9cc67582-…`), числовой `id` того же бренда **в базе каждого сервиса**
(в `cashier-aggregate-psp` он один, в `cashier-withdrawal` другой — JOIN между базами не
сделаешь), логин/пароль API-пользователя бренда и токен, который живёт час. Отсюда вечные
ошибки: пароль ищут в `cashier-api-gateway.user` (там только хэш, открытый — в
`cashier-accounts.account`), тело OAuth-запроса шлют form-urlencoded (сервер отвечает
`invalid_grant`), а `id` бренда подставляют из чужой базы и получают пустую выборку.

Здесь бренд берётся по uuid **или по имени** (WC1, Deposit, …), и всё остальное выводится
из него. Токен кэшируется файлом 0600 и освежается сам раньше, чем истечёт, — повторный
вызов API не тратит вход.

    python -m cashier_.cashier_brand find wc --env stage      # бренд по кусочку имени
    python -m cashier_.cashier_brand ids WC1 --env stage       # id бренда в базах сервисов
    python -m cashier_.cashier_brand token WC1 --env stage     # OAuth-токен (на экран — masked)

⚠ Пароль API-пользователя — из `cashier-accounts.account`, там он лежит открыто; на экран
и в чат он не выводится никогда, в git — тем более. Prod — только по прямой просьбе Хозяина.
⚠ Только чтение: файл ничего не пишет в базы и не меняет креды.
"""
import argparse
import json
import os
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

from cashier_.cashier_env import (cashier_env_database, cashier_env_headers, cashier_env_host,
                                  cashier_env_root)
from data_.data_mask import data_mask_json
from mysql_.mysql_query import mysql_query_run

# Куда кладётся токен: каталог 0700, файл 0600 — токен равен паролю по силе доступа.
CASHIER_BRAND_STORE = Path.home() / '.cache/cashier'

# Запас до конца жизни токена, после которого берётся новый (живёт он 3600 с).
CASHIER_BRAND_TOKEN_MARGIN = 120

# Сервисы, где у бренда есть своя строка `account` со своим числовым id.
CASHIER_BRAND_SERVICES = ('accounts', 'aggregate-psp', 'apm', 'withdrawal', 'deposit')


def cashier_brand_find(name: str, env: str = 'stage', root: str = '', limit: int = 30) -> list:
    """
    Найти бренд по имени (можно кусочком) — uuid, имя и состояние из `cashier-accounts`.

    Имена в списке регистронезависимы; `state` — ACTIVE/…, по нему видно, что бренд
    не выключен. Нужен перед любым «под каким брендом это подключено».
    """
    rows = _cashier_brand_rows(env, root, 'SELECT uuid, name, state FROM account '
                                          'WHERE LOWER(name) LIKE %s ORDER BY name',
                               (f'%{(name or "").lower()}%',), database='cashier-accounts', limit=limit)
    return rows


def cashier_brand_uuid(account: str, env: str = 'stage', root: str = '') -> str:
    """
    UUID бренда по uuid (как есть) или по имени; неоднозначное имя — ошибка со списком.

    Нужен всем, кто принимает «бренд» параметром: человек знает имя, базы знают uuid.
    """
    text = (account or '').strip()
    if _cashier_brand_looks_uuid(text):
        return text
    found = cashier_brand_find(text, env=env, root=root)
    exact = [row for row in found if str(row.get('name', '')).lower() == text.lower()]
    if len(exact) == 1:
        return exact[0]['uuid']
    raise RuntimeError(f'бренд {account!r} в {env} не опознан: '
                       f'{", ".join(str(row.get("name")) for row in found[:15]) or "ни одного совпадения"}')


def cashier_brand_ids(account: str, env: str = 'stage', root: str = '') -> dict:
    """
    Числовой id бренда в базе каждого сервиса — без него любой запрос к чужой базе пустой.

    Возвращает `{'uuid': …, 'cashier-accounts': 12, 'cashier-aggregate-psp': 34, …}`;
    сервиса нет в списке или бренда там нет — ключа нет. Каждый сервис держит свою таблицу
    `account` со своим автоинкрементом: uuid общий, id — нет.
    """
    uuid = cashier_brand_uuid(account, env=env, root=root)
    return dict(cashier_brand_uuid=uuid,
                **{database: row['id'] for database, row in
                   _cashier_brand_accounts(env, root, uuid, 'id').items()})


def cashier_brand_creds(account: str, env: str = 'stage', root: str = '') -> dict:
    """
    Логин и пароль API-пользователя бренда (`api_gateway_username/password`).

    Креды лежат открыто в `cashier-accounts.account`; в `cashier-api-gateway.user` — только
    их хэш, поэтому оттуда получить нечего. Возвращает `{'username','password'}`; на экран
    значение не выводится (маска — `data_mask_json`).

    Raises:
        RuntimeError: бренда нет или API-пользователь не заведён.
    """
    uuid = cashier_brand_uuid(account, env=env, root=root)
    rows = _cashier_brand_rows(env, root, 'SELECT api_gateway_username AS username, '
                                          'api_gateway_password AS password FROM account WHERE uuid = %s',
                               (uuid,), database='cashier-accounts', limit=1)
    if not rows:
        raise RuntimeError(f'бренда {uuid} в {env} нет')
    creds = rows[0]
    if not creds.get('username') or not creds.get('password'):
        raise RuntimeError(f'у бренда {uuid} нет API-пользователя: '
                           'api_gateway_username/password пустые (заводи в дашборде, Brands)')
    return creds


def cashier_brand_token(account: str, env: str = 'stage', root: str = '', store: str = '',
                        fresh: bool = False) -> dict:
    """
    OAuth-токен бренда для внешнего API — из кэша; истёк или его просрочили — вход заново.

    `POST https://api.<среда>ipaygateway.com/oauth/v2/token` телом **JSON**
    `{grant_type: "password", username, password}`; форма тот же пароль отвергает
    `invalid_grant`. `client_id`/`client_secret` не нужны: клиент OAuth один, берётся сам.

    Args:
        account: uuid или имя бренда.
        env: среда; `root` — чей `.env.local` читать ключи базы.
        store: куда ложить токен; `fresh` — не смотреть кэш.

    Returns:
        `{'token', 'token_type', 'expires_in', 'obtained', 'expires', 'brand', 'env'}` —
        `access_token` — полный; на печать он уходит замазанным, целиком — `--show-token`.

    Raises:
        RuntimeError: кредов нет или сервер отказал (текст ответа).
    """
    key = (env or 'stage').lower()
    saved = _cashier_brand_store_read(key, _cashier_brand_key(account), store)
    if saved.get('access_token') and not fresh and saved.get('expires', 0) - time.time() > CASHIER_BRAND_TOKEN_MARGIN:
        return saved
    uuid = cashier_brand_uuid(account, env=key, root=root)
    creds = cashier_brand_creds(uuid, env=key, root=root)
    token = _cashier_brand_fetch_token(uuid, creds['username'], creds['password'], env=key)
    _cashier_brand_store_write(token, store, _cashier_brand_key(account))
    _cashier_brand_store_write(token, store)
    return token


def cashier_brand_endpoints(account: str, env: str = 'stage', root: str = '') -> dict:
    """
    Куда бренд ждёт callbacks и каким ключом они подписаны — по каждой базе сервиса.

    `notification_uri`/`result_uri`/`signature_key` лежат в `account` каждого сервиса
    отдельно: у aggregate-psp одно, у withdrawal другое. Подпись callback-а —
    `X-Signature: hash_hmac('sha512', body, signature_key)`; проверять ей, а не угадывать.
    """
    uuid = cashier_brand_uuid(account, env=env, root=root)
    return dict(cashier_brand_uuid=uuid,
                **_cashier_brand_accounts(env, root, uuid, 'notification_uri, result_uri, signature_key'))


# ------------------------------------------------------------------- приватные функции

def _cashier_brand_rows(env: str, root: str, sql: str, params: tuple, database: str,
                        limit: int = 0) -> list:
    """Один читающий запрос к базе среды; ошибки `mysql_query` как есть (там есть «VPN?»)."""
    return mysql_query_run(sql, params, limit=limit, root=cashier_env_root(root),
                           profile=(env or 'stage').lower(), database=database)['rows']


def _cashier_brand_accounts(env: str, root: str, uuid: str, columns: str,
                            services: tuple = CASHIER_BRAND_SERVICES) -> dict:
    """
    Строка бренда сразу во всех базах — одним запросом: базы лежат рядом, на одном сервере.

    `columns` — колонки `account` через запятую; база, где такой колонки нет, в ответ не
    попадает (а не роняет весь запрос). Возвращает `{имя_базы: {колонка: значение}}`.
    """
    cols = [column.strip() for column in columns.split(',') if column.strip()]
    names = []
    for service in services:
        database = cashier_env_database(service)
        if database not in names:
            names.append(database)
    marks = ', '.join(['%s'] * len(names))
    known = _cashier_brand_rows(env, root,
                                f'SELECT TABLE_SCHEMA AS name, COLUMN_NAME AS field '
                                f'FROM information_schema.COLUMNS '
                                f'WHERE TABLE_SCHEMA IN ({marks}) AND TABLE_NAME = %s',
                                tuple(names) + ('account',), database='information_schema')
    fields = {}
    for row in known:
        fields.setdefault(row['name'], set()).add(str(row['field']).lower())
    live = [name for name in names if set(column.lower() for column in cols) <= fields.get(name, set())]
    if not live:
        return {}
    sql = ' UNION ALL '.join(
        f"SELECT '{name}' AS service, {', '.join(f'`{column}`' for column in cols)} "
        f'FROM `{name}`.account WHERE uuid = %s' for name in live)
    rows = _cashier_brand_rows(env, root, sql, tuple([uuid] * len(live)), database=live[0])
    return {row['service']: {column: row[column] for column in cols} for row in rows}


def _cashier_brand_looks_uuid(text: str) -> bool:
    return len(text) == 36 and text.count('-') == 4


def _cashier_brand_fetch_token(brand: str, username: str, password: str,
                               env: str = 'stage') -> dict:
    """Обмен кредов бренда на Bearer — телом JSON; форма тот же пароль отвергает invalid_grant."""
    url = cashier_env_host(env, 'api') + '/oauth/v2/token'
    body = {'grant_type': 'password', 'username': username, 'password': password}
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=cashier_env_headers())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text, status = response.read().decode(errors='replace'), response.status
    except urllib.error.HTTPError as err:
        raise RuntimeError(f'{url} ответил {err.code}: {err.read().decode(errors="replace")[:200]}')
    except urllib.error.URLError as err:
        raise RuntimeError(f'{url} не отвечает: {err.reason} (VPN? верна ли среда {env}?)')
    try:
        got = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f'{url} ответил не-JSON (HTTP {status}): {text[:200]}')
    if not got.get('access_token'):
        raise RuntimeError(f'токена нет (HTTP {status}): {text[:200]}')
    lives = int(got.get('expires_in') or 3600)
    return {'env': env, 'brand': brand, 'access_token': got['access_token'],
            'token_type': got.get('token_type', 'Bearer'), 'expires_in': lives,
            'obtained': int(time.time()), 'expires': int(time.time()) + lives}


def _cashier_brand_key(account: str) -> str:
    """Имя бренда в имени файла кэша: `WC1` и `wc1` — один и тот же бренд."""
    return (account or '').strip().lower()


def _cashier_brand_store_path(env: str, key: str, store: str = '') -> Path:
    return Path(store) if store else CASHIER_BRAND_STORE / f'token-{env}-{key}.json'


def _cashier_brand_store_read(env: str, key: str, store: str = '') -> dict:
    try:
        return json.loads(_cashier_brand_store_path(env, key, store).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _cashier_brand_store_write(token: dict, store: str = '', key: str = '') -> str:
    path = _cashier_brand_store_path(token['env'], key or token['brand'], store)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    path.write_text(json.dumps(token, ensure_ascii=False))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return str(path)


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description='бренд Cashier: uuid, id по базам, креды API, токен')
    _P.add_argument('command', nargs='?', default='find',
                    choices=('find', 'ids', 'token', 'endpoints', 'creds'),
                    help='что взять по бренду')
    _P.add_argument('account', nargs='?', default='', help='имя или uuid бренда')
    _P.add_argument('--env', default='stage')
    _P.add_argument('--root', default='', help='checkout, чей .env.local читать')
    _P.add_argument('--store', default='', help='куда ложить токен')
    _P.add_argument('--fresh', action='store_true', help='токен взять заново, не смотря в кэш')
    _P.add_argument('--show-token', action='store_true', help='напечатать токен целиком (для curl | …)')
    _C = _P.parse_args()
    try:
        if _C.command == 'find':
            for row in cashier_brand_find(_C.account, env=_C.env, root=_C.root):
                print(f"{row['uuid']}  {row['name']:<24} {row.get('state', '')}")
        elif _C.command == 'token':
            got = cashier_brand_token(_C.account, env=_C.env, root=_C.root, store=_C.store, fresh=_C.fresh)
            if not _C.show_token:
                got = data_mask_json(got)[0] or got
            got['lives'] = int(got['expires'] - time.time())
            print(json.dumps(got, ensure_ascii=False, indent=2))
        elif _C.command == 'endpoints':
            print(json.dumps(data_mask_json(cashier_brand_endpoints(_C.account, env=_C.env,
                                                                    root=_C.root))[0],
                             ensure_ascii=False, indent=2))
        elif _C.command == 'creds':
            print(json.dumps(data_mask_json(cashier_brand_creds(_C.account, env=_C.env,
                                                                root=_C.root))[0],
                             ensure_ascii=False, indent=2))
        else:
            print(json.dumps(cashier_brand_ids(_C.account, env=_C.env, root=_C.root),
                             ensure_ascii=False, indent=2))
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')

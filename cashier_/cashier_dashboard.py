"""
Дашборд Cashier через GraphQL: вход, смена бренда и запрос — одной строкой, без браузера.

Дашборд (`{env}ipaygateway.com`) говорит с бэкендом единственным диалектом: `POST /query`
на Go-фасад `cashier-service-admin-api-gateway`. Руками это три движения и две грабли:
учётку надо сначала достать из `.env.local`, куку `SESSID` — поймать из ответа `loginUser`
и перенести в следующий вызов, а бренд сессии после входа — не тот, что нужен задаче
(дежурит первый из списка), и запрос без `switchAccount` уходит в чужой бренд, честно
возвращая чужие данные. Каждый такой цикл стоит несколько вызовов и один вопрос «почему
тут пустая таблица».

Здесь цикл собран в один вход: учётка читается из `.env.local` по ключу
`CASHIER_UI_<СРЕДА>_EMAIL/PASSWORD`, сессия кладётся в файл 0600 и используется повторно,
истёкшая поднимается сама, бренд переключается по `--account` (uuid или имя). Ответ —
разобранный JSON, где `errors` это значение, а не исключение: зонд схемы
(`Cannot query field`) — нормальный рабочий ответ, его и надо видеть.

    python -m cashier_.cashier_dashboard stage                      # кто я и какой бренд
    python -m cashier_.cashier_dashboard stage \\
        '{ Clearers(filter:{processorName:["nuvei"]}){uuid alias active} }' --account WC1

Текущий бренд спрашивать у сервера не у чего: `currentAccount` есть только у ответа
`loginUser`, а `switchAccount` возвращает `success`. Поэтому бренд — своя часть файла
сессии, и он же возвращается в `account`.

⚠ Мутации отсюда уходят в **общее окружение**: `createClearer`, `withdrawalCreate…` меняют
конфиг брендов, который трогают все. Файл их не различает — правки на dev/stage только по
прямой просьбе Хозяина, на prod не ходит вовсе.
⚠ `Clearers` без фильтра отдаёт не больше 500 записей, и новый провайдер в них не попадает —
фильтр по `processorName` обязателен (AGENTS.md «Частые ловушки»).
"""
import argparse
import json
import os
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

from cashier_.cashier_env import (cashier_env_explain, cashier_env_headers, cashier_env_host,
                                  cashier_env_root)
from config.config import config_setting

# Кука сессии Go-фасада: `session/middleware.go`, CookieName.
CASHIER_DASHBOARD_COOKIE = 'SESSID'

# Куда ложится сессия: каталог 0700, файл 0600 — кука равна паролю по силе доступа.
CASHIER_DASHBOARD_STORE = Path.home() / '.cache/cashier'

# Ключ учётки в `.env.local`: среда и поле подставляются заглавными (CASHIER_UI_STAGE_EMAIL).
CASHIER_DASHBOARD_KEY = 'CASHIER_UI_{ENV}_{FIELD}'

# Признаки того, что сессия истекла, а не запрос плохой: после них имеет смысл войти заново.
CASHIER_DASHBOARD_EXPIRED = ('unauthenticated', 'unauthorized', 'token is expired',
                             'invalid session', 'session not found')

# Бренды, доступные этой учётке, — он же список для переключения и проверка «дошёл ли бренд».
CASHIER_DASHBOARD_ACCOUNTS = '{ Accounts { uuid name state } }'


def cashier_dashboard_login(env: str = 'stage', email: str = '', password: str = '',
                            root: str = '', store: str = '') -> dict:
    """
    Войти в дашборд и вернуть профиль с кукой сессии; учётка — из `.env.local`.

    Args:
        env: `dev`, `stage`, `prod`.
        email/password: пусто — взять `CASHIER_UI_<СРЕДА>_EMAIL/PASSWORD`.
        root: checkout, чей `.env.local` читать; пусто — найти самому (`cashier_env_root`).
        store: куда положить сессию; пусто — `~/.cache/cashier/dashboard-<среда>.json`.

    Returns:
        `{'account': {uuid,name}, 'user': {email}, 'cookies': {SESSID: …}}`.

    Raises:
        RuntimeError: ключей нет, хост не ответил или вход отвергнут (текст ошибки GraphQL).
    """
    key = (env or 'stage').lower()
    source = cashier_env_root(root)
    email = email or config_setting(CASHIER_DASHBOARD_KEY.format(ENV=key.upper(), FIELD='EMAIL'),
                                    root=source)
    password = password or config_setting(CASHIER_DASHBOARD_KEY.format(ENV=key.upper(), FIELD='PASSWORD'),
                                          root=source)
    if not email or not password:
        raise RuntimeError(
            f'учётки дашборда {key} нет: ключа '
            f'{CASHIER_DASHBOARD_KEY.format(ENV=key.upper(), FIELD="EMAIL")} не найдено '
            f'(читал {source or "окружение процесса"})')
    got = _cashier_dashboard_post(key, 'mutation($e: String!, $p: String!) '
                                       '{ loginUser(email: $e, password: $p) '
                                       '{ currentAccount { uuid name } user { email } } }',
                                  {'e': email, 'p': password})
    login = (got.get('data') or {}).get('loginUser')
    if not login:
        raise RuntimeError(f'вход в дашборд {key} не удался: {_cashier_dashboard_errors(got)}')
    session = {'env': key, 'cookies': got['cookies'], 'account': login.get('currentAccount') or {},
               'user': login.get('user') or {}, 'obtained': int(time.time())}
    _cashier_dashboard_store_write(session, store)
    return {'account': session['account'], 'user': session['user'], 'cookies': session['cookies']}


def cashier_dashboard_session(env: str = 'stage', root: str = '', store: str = '') -> dict:
    """
    Живая сессия дашборда: сохранённая, а её нет — свежий вход.

    Возвращает `{'cookies': …, 'account': {uuid,name}, 'fresh': bool}`; `fresh` —
    пришлось войти, а не достать из файла.
    """
    key = (env or 'stage').lower()
    saved = _cashier_dashboard_store_read(key, store)
    if saved and saved.get('cookies'):
        return {'cookies': saved['cookies'], 'account': saved.get('account') or {}, 'fresh': False}
    got = cashier_dashboard_login(env=key, root=root, store=store)
    return {'cookies': got['cookies'], 'account': got['account'], 'fresh': True}


def cashier_dashboard_accounts(env: str = 'stage', root: str = '', store: str = '') -> list:
    """
    Бренды, доступные этой учётке (`Accounts`) — чем переключаются и что ищется по имени.

    Короткий и точный список: в него входят бренды, на которые у пользователя есть права,
    — в отличие от таблицы `account`, где их сотни.
    """
    session = cashier_dashboard_session(env=env, root=root, store=store)
    got = _cashier_dashboard_post(env, CASHIER_DASHBOARD_ACCOUNTS, None, session['cookies'])
    if got['errors']:
        raise RuntimeError(f'список брендов не сошёл: {_cashier_dashboard_errors(got)}')
    return (got.get('data') or {}).get('Accounts') or []


def cashier_dashboard_switch(account: str, env: str = 'stage', root: str = '', store: str = '') -> dict:
    """
    Переключить бренд сессии (`switchAccount`) и вернуть его; принимает uuid или имя.

    Нужен всегда, когда задача про конкретный бренд: после входа текущий — первый из
    списка, а не нужный; адрес `/wc1/...` в браузере меняет шапку, а не данные.

    Raises:
        RuntimeError: бренда нет среди доступных (имя неразборчиво) либо `success: false`.
    """
    accounts = cashier_dashboard_accounts(env, root, store)
    wanted = _cashier_dashboard_pick(account, accounts)
    session = cashier_dashboard_session(env=env, root=root, store=store)
    got = _cashier_dashboard_post(env, 'mutation($u: ID!) { switchAccount(uuid: $u) { success } }',
                                 {'u': wanted['uuid']}, session['cookies'])
    if not ((got.get('data') or {}).get('switchAccount') or {}).get('success'):
        raise RuntimeError(f'бренд {account!r} не взят: {_cashier_dashboard_errors(got) or "success=false"}')
    _cashier_dashboard_store_account(env, wanted, store)
    return wanted


def cashier_dashboard_call(query: str, variables: dict | None = None, env: str = 'stage',
                           account: str = '', root: str = '', store: str = '') -> dict:
    """
    Один GraphQL-запрос к дашборду со своим входом и своим брендом — без curl и cookie jar.

    Сессию берёт сохранённую (или входит), при `account` — переключается, на истёкшей
    сессии входит один раз заново. Ошибки схемы в `errors` — нормальный ответ, не исключение.

    Args:
        query: текст запроса или мутации, как есть (кавычки не экранировать — не шелл).
        variables: значения для `$переменных`.
        env: среда; `account` — uuid или имя бренда, на который переключиться.
        root: чей `.env.local` читать; `store` — где лежит сессия.

    Returns:
        `{'data': …, 'errors': […], 'account': {uuid,name}}` — бренд тот, на котором запрос
        фактически исполнился.

    Raises:
        RuntimeError: войти не удалось или бренд не переключился.
    """
    session = cashier_dashboard_session(env=env, root=root, store=store)
    brand = session['account']
    wanted = account.strip().lower()
    # Бренд уже взят — по uuid или по имени; переключать нечего, и список брендов не нужен.
    if wanted and wanted not in {str(brand.get('uuid', '')).lower(), str(brand.get('name', '')).lower()}:
        brand = cashier_dashboard_switch(account, env=env, root=root, store=store)
        session = cashier_dashboard_session(env=env, root=root, store=store)
    got = _cashier_dashboard_post(env, query, variables, session['cookies'])
    if got['errors'] and _cashier_dashboard_is_expired(got):
        # Сессия умерла между вызовами: один вход заново, повтор того же запроса.
        cashier_dashboard_login(env=env, root=root, store=store)
        if brand.get('uuid'):
            brand = cashier_dashboard_switch(brand['uuid'], env=env, root=root, store=store)
        session = cashier_dashboard_session(env=env, root=root, store=store)
        got = _cashier_dashboard_post(env, query, variables, session['cookies'])
    return {'data': got.get('data'), 'errors': got.get('errors') or [], 'account': brand}


# ------------------------------------------------------------------- приватные функции

def _cashier_dashboard_post(env: str, query: str, variables: dict | None = None,
                            cookies: dict | None = None, timeout: float = 40) -> dict:
    """POST /query; `{'data','errors','cookies','status'}` — куки ответа долиты к отправленным."""
    url = cashier_env_host(env, 'dashboard') + '/query'
    body = {'query': query}
    if variables:
        body['variables'] = variables
    extra = {'Cookie': '; '.join(f'{k}={v}' for k, v in cookies.items())} if cookies else None
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                    headers=cashier_env_headers(extra))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode(errors='replace')
            status, headers_out = response.status, response.headers
    except urllib.error.HTTPError as err:
        text, status, headers_out = err.read().decode(errors='replace'), err.code, err.headers
    except urllib.error.URLError as err:
        raise RuntimeError(f'{url} не отвечает: {err.reason} (VPN? верна ли среда {env}?)')
    try:
        got = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(cashier_env_explain(status, text, url))
    jar = dict(cookies or {})
    for line in headers_out.get_all('Set-Cookie') or []:
        name, _, value = line.split(';')[0].partition('=')
        if value:
            jar[name.strip()] = value.strip()
    return {'data': got.get('data'), 'errors': got.get('errors') or [], 'cookies': jar, 'status': status}


def _cashier_dashboard_store_path(env: str, store: str = '') -> Path:
    return Path(store) if store else CASHIER_DASHBOARD_STORE / f'dashboard-{env}.json'


def _cashier_dashboard_store_read(env: str, store: str = '') -> dict:
    try:
        return json.loads(_cashier_dashboard_store_path(env, store).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _cashier_dashboard_store_write(session: dict, store: str = '') -> str:
    path = _cashier_dashboard_store_path(session.get('env', 'stage'), store)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    path.write_text(json.dumps(session, ensure_ascii=False))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return str(path)


def _cashier_dashboard_store_account(env: str, account: dict, store: str = '') -> None:
    """Записать переключенный бренд в файл сессии: он и есть «текущий бренд»."""
    saved = _cashier_dashboard_store_read(env, store)
    saved.update({'env': env, 'account': {'uuid': account['uuid'], 'name': account.get('name', '')},
                  'obtained': int(time.time())})
    _cashier_dashboard_store_write(saved, store)


def _cashier_dashboard_pick(account: str, accounts: list) -> dict:
    """Найти бренд в доступных — сперва по uuid, потом по имени; иначе внятная ошибка."""
    wanted = (account or '').strip().lower()
    for row in accounts:
        if str(row.get('uuid', '')).lower() == wanted:
            return row
    for row in accounts:
        if str(row.get('name', '')).lower() == wanted:
            return row
    named = [str(row.get('name', '?')) for row in accounts]
    raise RuntimeError(f'бренда {account!r} среди доступных нет; бывает {", ".join(named[:20])}'
                       + ('…' if len(named) > 20 else ''))


def _cashier_dashboard_errors(got: dict) -> str:
    return '; '.join(str(item.get('message', item)) for item in got.get('errors') or [])[:300]


def _cashier_dashboard_is_expired(got: dict) -> bool:
    return any(sign in _cashier_dashboard_errors(got).lower() for sign in CASHIER_DASHBOARD_EXPIRED)


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description='GraphQL дашборда Cashier со своим входом и брендом')
    _P.add_argument('query', nargs='?', default='', help='текст запроса; пусто — кто я и мой бренд')
    _P.add_argument('env', nargs='?', default='stage')
    _P.add_argument('--account', default='', help='uuid или имя бренда для switchAccount')
    _P.add_argument('--vars', default='', help='JSON с переменными запроса')
    _P.add_argument('--accounts', action='store_true', help='показать доступные бренды')
    _P.add_argument('--switch', default='', help='только переключить бренд')
    _P.add_argument('--root', default='', help='checkout, чей .env.local читать')
    _P.add_argument('--store', default='', help='куда положить сессию')
    _C = _P.parse_args()
    try:
        if _C.accounts:
            for row in cashier_dashboard_accounts(env=_C.env, root=_C.root, store=_C.store):
                print(f"{row['uuid']}  {row['name']:<24} {row['state']}")
        elif _C.switch:
            print(json.dumps(cashier_dashboard_switch(_C.switch, env=_C.env, root=_C.root,
                                                     store=_C.store), ensure_ascii=False))
        else:
            answer = cashier_dashboard_call(_C.query or CASHIER_DASHBOARD_ACCOUNTS,
                                           json.loads(_C.vars) if _C.vars else None, env=_C.env,
                                           account=_C.account, root=_C.root, store=_C.store)
            if _C.query:
                print(json.dumps(answer, ensure_ascii=False, indent=2))
            else:
                print(f"бренд {answer['account'].get('name', '?')} "
                      f"({answer['account'].get('uuid', '?')}), брендов в доступе "
                      f"{len((answer['data'] or {}).get('Accounts') or [])}")
            if answer['errors']:
                raise SystemExit(1)
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')

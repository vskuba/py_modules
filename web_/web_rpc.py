"""
Приватные API сайтов под сессией управляемого браузера: extract секретов из хранилищ, реплей RPC.

«В браузере логинился, а агент не может: fetch из консоли браузера не годен —
CSP, а из Python — 403 (cf-mit, Cloudflare)» — вход: сырой скан leveldb-.log и
профилейных хранилищ, и тот же секрет в свой HTTP-клиент. Логины в закладках
не пишутся в LocalStorage/IndexedDB, а «token в value BLOB'а лежит raw байтами,
typeof(value) врет 'text'» — JSON-разбор такие Blob'ы не читает, потому что Blob
не JSON; потому скан байтовый.

«Из Python-скрипта fetch() в страснице не годен — CORS» и «нужен вызов чужого
RPC от своего HTTP-клиента» — web_rpc_call: параметра чужих RPC уходят в
query-строку («параметры в query string + токен в JSON-теле», иначе
`missing required field: channel`), а токен — и в query, и в тело, и в
заголошок `Authorization: Bearer` — как диктует метод.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from browser_.browser_profile import browser_profile_cookies_db, browser_profile_extract, browser_profile_read_cookies
from sqllite3.sqllite3_ import sqllite3_copy_readonly, sqllite3_read_localstorage

# ── константы ──

WEB_RPC_SECRET_PATTERNS = (rb'xox[abprde]-[A-Za-z0-9\-]{16,}',
                           rb'(?:dc-|hc-|ct-|ssda-|ss-)[A-Za-z0-9.\-_]{16,}',
                           rb'[?&"](?:d|b|x|ss)=\d{10,}-[A-Za-z0-9]{8,}',
                           rb'[Bb]earer\s+[A-Za-z0-9._\-]{16,}')  # «token в value лежит raw байтами»
WEB_RPC_LEVELDB_FILES = ('LOG', 'LOG-OLD', 'MANIFEST-000003')  # .log/.ldb чужих хранилищ
WEB_RPC_TMP_DIR = '/tmp/.dsh-web-rpc'  # каталог read-only копий хранилищ

# ── публичный API модуля ──


def web_rpc_extract_secret(path: str, patterns: tuple = WEB_RPC_SECRET_PATTERNS,
                           value_prefix_len: int = 10, tmp_dir: str = WEB_RPC_TMP_DIR) -> tuple:
    """Сырой скан хранилищ браузера: найти сессионные токены raw-байтами, без CSP/CORS.

    «Token в value BLOB'а лежит raw байтами, typeof() врет 'text' — JSON-разбор
    не читает Blob» — потому скан байтовый, а не JSON-ный; path — файл (.log,
    data.sqlite), каталог leveldb либо корень persistent-профиля.

    Args:
        path: файл хранилища, каталог leveldb либо каталог профиля.
        patterns: regex-ы секретов (bytes); пустой кортеж — скан не вести.
        value_prefix_len: сколько символов значения возвращать (секреты не плодят).
        tmp_dir: каталог для read-only копий.

    Returns:
        ([{'file', 'key', 'offset', 'length', 'value_prefix'}], None) — полные
        токены наружу не кладут; либо (None, 'текст ошибки').
    """
    if not os.path.exists(path):
        return None, f'нет пути: {path}'
    found = []
    for target in _web_rpc_secret_targets(path):
        is_store = os.path.basename(target) in ('data.sqlite', 'idb', 'data-v3.sqlite',
                                                'cookies.sqlite', 'Cookies', 'Cookies-journal')
        if is_store:
            rows, err = _web_rpc_scan_store(target, patterns, value_prefix_len, tmp_dir)
        else:
            rows, err = _web_rpc_scan_raw(target, patterns, value_prefix_len)
        if rows is None:
            return None, err
        for row in rows:
            row['file'] = target
            found.append(row)
    return found, None


def web_rpc_extract_localstorage(profile_dir: str, origin: str = '', key_prefix: str = '',
                                  tmp_dir: str = WEB_RPC_TMP_DIR) -> tuple:
    """Вытащить kv из localStorage профиля (raw-скан BLOB'ов; данные — в sqllite3_).

    Args:
        profile_dir: каталог persistent-профиля.
        origin: хост сайта в пути storage/default/https+++<host>; '' — первый origin.
        key_prefix: оставить только ключи с этим началом ('xoxc-', 'ct-'); пусто — все.
        tmp_dir: каталог для read-only копий.

    Returns:
        ({ключ: значение}, None) либо (None, 'текст ошибки').
    """
    if origin:
        ls_db = os.path.join(profile_dir, 'storage', 'default',
                             origin.replace('://', '+++'), 'ls', 'data.sqlite')
        if not os.path.isfile(ls_db):
            return None, f'у origin {origin!r} нет хранилища {ls_db}'
    else:
        ls_db, err = _web_rpc_first_localstorage(profile_dir)
        if ls_db is None:
            return None, err
    kv, err = sqllite3_read_localstorage(ls_db)
    if err:
        return None, err
    if key_prefix:
        kv = {k: v for k, v in kv.items() if k.startswith(key_prefix)}
    return kv, None


def web_rpc_extract_cookies(profile_dir: str, names: tuple = ('d', 'b', 'x', 'ss'),
                            tmp_dir: str = WEB_RPC_TMP_DIR) -> tuple:
    """Вытащить куки профиля (moz_cookies у Firefox, host_key у Chromium).

    Args:
        profile_dir: каталог persistent-профиля.
        names: имена кук; пусто — все.
        tmp_dir: каталог для read-only копий.

    Returns:
        ([{'name', 'host', 'value_prefix'}], None) либо (None, 'текст ошибки').
    """
    db = _web_rpc_cookies_db(profile_dir)
    if not db:
        return None, f'в профиле нет кук (cookies.sqlite/Cookies): {profile_dir}'
    return browser_profile_read_cookies(db, names, tmp_dir)


def web_rpc_call(url: str, token: str = '', cookies: dict = None, params: dict = None,
                 method: str = 'POST', json_body: dict = None, token_in_body_key: str = 'token',
                 origin: str = 'https://app.slack.com', referer: str = 'https://app.slack.com/',
                 timeout: int = 30) -> tuple:
    """Вызвать чужой RPC от своего HTTP-клиента, минуя браузер: query+body+cookies.

    «Параметры чужих RPC в query string + токен в JSON-теле (иначе
    `missing required field: channel`); Content-Type без `charset=utf-8` дает
    `invalid_json`; `latest=true` без `oldest` — `invalid_ts_latest`» — всё учтено.
    `token_in_body_key=''` — токен уходит в заголошок `Authorization: Bearer`.

    Args:
        url: базовый URL метода (…/api/conversations.history).
        token: сессионный токен (xoxc-…); в тело/заголошок по token_in_body_key.
        cookies: {'d': …, 'b': …} — уходят в заголошок Cookie и в тело по именам.
        params: параметра метода — уходят в query-строку (не в тело!).
        method: 'POST' либо 'GET'.
        json_body: тело JSON; None — собрать из params+cookies+token.
        token_in_body_key: имя ключа токена в теле ('' — не класть, класть в Bearer).
        origin, referer: заголошки Origin/Referer (web-client их требует).
        timeout: таймаут коннекта, сек.

    Returns:
        (parsed, None) — parsed dict|str; либо (None, 'текст ошибки с телом ответа').
    """
    query = {k: str(v) for k, v in (params or {}).items()}
    if method.upper() == 'GET' and token:
        query = {'token': token, **query}
    url_full = url + ('?' + urllib.parse.urlencode(query) if query else '')
    body = dict(json_body or {})
    if method.upper() == 'POST':
        body = body or dict(params or {})
        if token_in_body_key and token:
            body[token_in_body_key] = token
        for name, value in (cookies or {}).items():
            if value:
                body[name] = value
    headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20231130 Firefox/121.0',
               'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.5',
               'Accept-Encoding': 'gzip, deflate', 'DNT': '1',
               'Origin': origin, 'Referer': referer or origin}
    if not token_in_body_key and token:
        headers['Authorization'] = f'Bearer {token}'
    if cookies:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items() if v)
    raw = None
    if method.upper() == 'POST':
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json;charset=utf-8'  # без charset — invalid_json
    request = urllib.request.Request(url_full, data=raw, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as err:
        return None, f'{method} {url_full.split("?")[0]} -> HTTP {err.code}: ' \
                     f'{err.read().decode("utf-8", "ignore")[:240]}'
    except urllib.error.URLError as err:
        return None, f'{method} {url_full.split("?")[0]} -> не достать: {err.reason}'
    except OSError as err:
        return None, f'{method} {url_full.split("?")[0]} -> обрыв коннекта: {err}'
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict) and parsed.get('ok') is False:
            return parsed, f'сайт ответил ok=false: {str(parsed.get("error"))[:160]}'
        return parsed, None
    except ValueError:
        return payload, None



# ── детали реализации ──


def _web_rpc_secret_targets(path: str) -> list:
    """Собрать файлы-цели raw-скана: leveldb/.log/data.sqlite рядом с path либо из профиля."""
    if os.path.isfile(path):
        return [path]
    targets = []
    for walk_root, dirs, files in os.walk(path):
        for name in sorted(files):
            if name.startswith('LOG') or name.startswith('MANIFEST-') or name.endswith('.ldb') \
                    or name in WEB_RPC_LEVELDB_FILES or name in ('data.sqlite', 'idb', 'data-v3.sqlite') \
                    or name in ('cookies.sqlite', 'Cookies') or name.endswith('cookies.sqlite'):
                targets.append(os.path.join(walk_root, name))
    return list(dict.fromkeys(targets))


def _web_rpc_scan_raw(target: str, patterns: tuple, value_prefix_len: int) -> tuple:
    """Raw-скан одного файла: regex-ы по сырым байтам, находки с обрезанным значением."""
    try:
        blob = open(target, 'rb').read()
    except OSError as err:
        return None, f'хранилище не читается ({target}): {err}'
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, blob):
            found.append({'file': target, 'key': '', 'offset': match.start(),
                          'length': len(match.group(0)),
                          'value_prefix': match.group(0)[:value_prefix_len].decode('utf-8', 'ignore')})
    return found, None


def _web_rpc_scan_store(db_path: str, patterns: tuple, value_prefix_len: int,
                        tmp_dir: str) -> tuple:
    """Raw-скан sqlite-хранилища: копия в tmp, regex по сырым байтам (декарт sqllite3_)."""
    tmp_path, err = sqllite3_copy_readonly(db_path, tmp_dir)
    if tmp_path is None:
        return None, err
    found, err = _web_rpc_scan_raw(tmp_path, patterns, value_prefix_len)
    if found is None:
        return None, err
    for row in found:
        row['key'] = ''  # у BLOB'ов ключ в начале Blob'а — см. sqllite3_scan_blob
        row['file'] = db_path
    return found, None


def _web_rpc_first_localstorage(profile_dir: str) -> tuple:
    """Первый data.sqlite профиля (когда origin не назван): обход storage/default/*."""
    default = os.path.join(profile_dir, 'storage', 'default')
    try:
        for origin_dir in sorted(os.scandir(default), key=lambda e: e.path):
            if origin_dir.is_dir() and os.path.isfile(os.path.join(origin_dir.path, 'ls', 'data.sqlite')):
                return os.path.join(origin_dir.path, 'ls', 'data.sqlite'), None
    except OSError as err:
        return None, f'storage/default не читается ({default}): {err}'
    return None, f'в {default} нет ls/data.sqlite'


def _web_rpc_cookies_db(profile_dir: str) -> str:
    """Найти в профиле файл кук (прокид к browser_profile_; имена — см. там же)."""
    return browser_profile_cookies_db(profile_dir)


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('scan', 'localstorage', 'cookies'), help='что показать')
    _P.add_argument('--path', required=True, help='профиль/leveldb/data.sqlite')
    _P.add_argument('--origin', default='', help='хост сайта в storage, напр. app.slack.com')
    _C = _P.parse_args()
    if _C.cmd == 'scan':
        _rows, _err = web_rpc_extract_secret(_C.path)
        if _rows is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _r in _rows:
            print(f"{_r['file']} [{_r['key'][:48]}] offset={_r['offset']} len={_r['length']} "
                  f"value={_r['value_prefix']!r}…")
    elif _C.cmd == 'localstorage':
        _kv, _err = web_rpc_extract_localstorage(_C.path, _C.origin)
        if _kv is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _k, _v in sorted(_kv.items()):
            print(f'{_k[:64]:<64} = {_v[:10]}… ({len(_v)}б)')
    else:
        _rows, _err = web_rpc_extract_cookies(_C.path)
        if _rows is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _r in _rows:
            print(f"{_r['name']:<24} {_r['host']:<28} {_r['value_prefix']!r}…")
    raise SystemExit(0)
"""
Slack-веб-клиент (api.slack.com/api/*) под сессию управляемого браузера: extract токенов, реплей RPC.

«Логин в управляемом Firefox был, а агент ходит в сайт уже своим HTTP-клиентом:
fetch из консоли браузера не годен (CSP `fetch` в console-eval не резолвится,
а из Python 403 cf-mit — Cloudflare)» — вход: вынуть сессионные токены из
профиля с диска и тем же клиентом реплейнить RPC. Токены и куки сессии —
секреты: в отпечатках и дампах наружу не более 10 символов значения, полные
значения — только в файлах 0600 и в аргументах вызова.

Хранилища Firefox (`data` BLOB'ы в `storage/default/https+++app.slack.com/ls/data.sqlite`,
куки в `cookies.sqlite`) читаются с read-only копий (см. `sqllite3/sqllite3_.py`);
BLOB-«data» — JSON-обёртка `{key, value, ms, db, dbf, pk}` (type 2), value лежит
raw-байтами и `typeof(value)` врет «text», потому value режется regex-ом по
сырым байтам read-only копии.
"""
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from browser_.browser_profile import browser_profile_cookies_db
from sqllite3.sqllite3_ import sqllite3_connect_readonly, sqllite3_copy_readonly

# ── константы ──

SLACK_API_BASE = 'https://api.slack.com/api'  # api.slack.com/api/<method>, НЕ slack.com
SLACK_WEB_BASE = 'https://slack.com'  # Origin/Referer управляемого браузера
SLACK_TOKEN_DIR = '/tmp/.dsh-slack'  # каталог тикетницы; каталог 0700, файлы 0600
SLACK_TOKEN_DIR_MODE = 0o700
SLACK_TOKEN_FILE = 'tokens'  # имя файла токенов в каталоге
SLACK_TOKEN_MODE = 0o600
SLACK_TOKEN_PREFIX_LEN = 10  # «значение обрезать до <=10 символов»
SLACK_TMP_DIR = '/tmp/.dsh-slack-profile'  # read-only копии хранилищ
SLACK_TOKEN_KEYS = ('xoxc', 'xoxb', 'xoxp', 'xoxa', 'xoxe', 'xoxd', 'd', 'b', 'x', 'ss')
# «channel >100k сообщений → list=2000+ сообщений не впихнуть в один
#  tool_execute — реплей по oldest+latest=true,inclusive=true пока не упёрся»
SLACK_HISTORY_LIMIT = 2000
SLACK_LS_DB_PATH = 'storage/default/https+++app.slack.com/ls/data.sqlite'
SLACK_COOKIE_NAMES = ('d', 'b', 'x', 'ss')  # имена кук сессии в cookies.sqlite
SLACK_SECRET_VALUE_RE = re.compile(
    r'(?P<secret>\b(?:xox[a-z]-[A-Za-z0-9\-]{16,}|(?:dc|hc|ct|ssda|ss)-[A-Za-z0-9.\-_]{16,}'
    r'|[dbxs]{1,2}=\d{10,}-[A-Za-z0-9]{8,}))')

# ── публичный API модуля ──


def slack_token_prefix(token: str, length: int = SLACK_TOKEN_PREFIX_LEN) -> str:
    """Префикс секрета для отпечатков/дампов: не более `length` первых символов."""
    if not isinstance(token, str):
        return ''
    return token[:length]


def slack_extract_tokens(profile_dir: str, host: str = 'app.slack.com',
                         cookie_names: 'tuple | list' = SLACK_COOKIE_NAMES,
                         tmp_dir: str = SLACK_TMP_DIR) -> tuple:
    """Вытащить сессионные токены управляемого браузера из persistent-профиля.

    «Логины в закладках и `auth-cookie` в закладках не пишутся, а `xoxc-…` —
    BLOB «data» (type 2) в `storage/default/https+++app.slack.com/ls/data.sqlite`,
    ключи в JSON-обёртке {key,value,ms,db,dbf,pk}; typeof(value) врет 'text'» —
    потому value режется regex-ом по сырым байтам read-only копии.

    Args:
        profile_dir: каталог persistent-профиля (`launchPersistentContext(USER_FIREFOX, ...)`).
        host: хост сайта в пути `storage/default/https+++<host>`.
        cookie_names: имена кук (`d`,`b`,`x`,`ss`) либо () — только BLOB-скан.
        tmp_dir: каталог read-only копий.

    Returns:
        ({'xoxc'|'xoxd'|'xoxa'|'xoxb'|'xoxe'|'d'|'b'|'x'|'ss': 'полный токен'}, None)
        — полные токены в возвращаемом dict; в отпечаток/дамп — только
        slack_token_prefix(); либо ({}, 'текст ошибки').
    """
    if not os.path.isdir(profile_dir):
        return {}, f'нет профиля: {profile_dir}'
    if not os.path.isdir(tmp_dir):
        try:
            os.makedirs(tmp_dir, mode=SLACK_TOKEN_DIR_MODE, exist_ok=True)
        except OSError as err:
            return {}, f'каталог {tmp_dir} не создан: {err}'
    tokens = {}
    misses = []
    ls_db, err = _slack_ls_db_path(profile_dir, host)
    if ls_db is None:
        return {}, err
    tmp, err = sqllite3_copy_readonly(ls_db, tmp_dir)
    if tmp is None:
        return {}, f'хранилище localStorage не копируется в read-only ({ls_db}): {err}'
    rows, err = _slack_scan_blob(tmp)
    if rows is None:
        return {}, err
    for kind, value in rows:
        tokens[kind] = value
    for name in cookie_names:
        found, err = slack_extract_cookie(profile_dir, name, host, tmp_dir)
        if found is None:
            return {}, err
        if found:
            tokens[name] = found
        else:
            misses.append(f'кука {name!r} не найдена')
    if not tokens:
        return {}, '; '.join(misses) if misses else f'в профиле нет токенов: {ls_db}'
    return tokens, None


def slack_extract_cookie(profile_dir: str, name: str, host: str = 'app.slack.com',
                         tmp_dir: str = SLACK_TMP_DIR) -> tuple:
    """Прочитать куку `d`/`b`/`x`/`ss` сессии (value raw-байтами в moz_cookies/cookies).

    Args:
        profile_dir: каталог persistent-профиля.
        name: имя куки (`d`, `b`, `x`, `ss`).
        host: хост сайта.
        tmp_dir: каталог read-only копии.

    Returns:
        (значение куки, None) либо (None, 'текст ошибки'); ('', None) — куки нет.
    """
    if not os.path.isdir(profile_dir):
        return None, f'нет профиля: {profile_dir}'
    db = browser_profile_cookies_db(profile_dir)
    if not db:
        return None, (f'в профиле {profile_dir} нет кук '
                      f'(ни cookies.sqlite, ни Network/Cookies)')
    conn, err = sqllite3_connect_readonly(db, tmp_dir)
    if conn is None:
        return None, f'хранилище кук не читается ({db}): {err}'
    try:
        cur = conn.cursor()
        if os.path.basename(db) == 'cookies.sqlite':  # Firefox: moz_cookies(host)
            table, host_col = 'moz_cookies', 'host'
        else:  # Chromium: cookies(host_key)
            table, host_col = 'cookies', 'host_key'
        cur.execute(f'SELECT value FROM "{table}" WHERE "{host_col}"=? AND name=?',
                    (host, name))
        row = cur.fetchone()
        if not row:
            return '', None
        return (row[0].decode('latin-1') if isinstance(row[0], bytes)
                else str(row[0] or '')), None
    except Exception as err:
        return None, f'хранилище кук не читается ({db}): {err}'
    finally:
        conn.close()


def slack_token_save(tokens: dict, directory: str = SLACK_TOKEN_DIR,
                     force: bool = False) -> tuple:
    """Сохранить токены файлом 0600 (каталог 0700) — «пароли/API-ключи в дамп не кладут».

    Args:
        tokens: {'xoxc': 'xoxc-…', 'd': '…'} — полные значения (секреты).
        directory: каталог тикетницы.
        force: перезаписать существующий файл.

    Returns:
        (путь, None) либо (None, 'текст ошибки').
    """
    if not tokens:
        return None, 'пустой набор токенов'
    bad = [key for key, value in tokens.items() if not value or not isinstance(value, str)]
    if bad:
        return None, f'пустые/не строковые значения у ключей: {", ".join(sorted(bad))}'
    try:
        os.makedirs(directory, mode=SLACK_TOKEN_DIR_MODE, exist_ok=True)
    except OSError as err:
        return None, f'каталог {directory} не создан: {err}'
    path = os.path.join(directory, SLACK_TOKEN_FILE)
    if os.path.exists(path) and not force:
        return None, f'{path} уже есть: force=True либо другой directory'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SLACK_TOKEN_MODE)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(''.join(f'{key}={value}\n' for key, value in sorted(tokens.items())))
    except OSError as err:
        return None, f'файл токенов не записан ({path}): {err}'
    return path, None


def slack_token_load(directory: str = SLACK_TOKEN_DIR) -> tuple:
    """Читать сохранённые токены (файл 0600): {префикс-ключ: полный токен}.

    Возвращает полные значения: в отпечаток/дамп — только slack_token_prefix().

    Args:
        directory: каталог тикетницы.

    Returns:
        (tokens, None) либо (None, 'текст ошибки').
    """
    path = os.path.join(directory, SLACK_TOKEN_FILE)
    if not os.path.isfile(path):
        return None, f'нет файла токенов: {path} (сначала slack_token_save либо --extract)'
    try:
        tokens = {}
        for line in open(path, 'r', encoding='utf-8').read().splitlines():
            if '=' in line and line.split('=', 1)[0].strip() in SLACK_TOKEN_KEYS:
                key, value = line.split('=', 1)
                tokens[key.strip()] = value
        if not tokens:
            return None, f'в {path} нет строк «<ключ>=<значение>»'
        return tokens, None
    except OSError as err:
        return None, f'файл токенов не читается ({path}): {err}'


def slack_api_call(method: str, token: str = '', params: dict = None,
                    d_cookie: str = '', b_cookie: str = '', x_cookie: str = '',
                    ss_cookie: str = '', base: str = SLACK_API_BASE, token_in_body: str = 'token',
                    timeout: int = 30) -> tuple:
    """Вызвать RPC Slack своим HTTP-клиентом (браузер не годен: CSP fetch + Cloudflare 403).

    «Параметры Slack в query string + токен в JSON-теле (иначе
    `missing required field: channel`); Content-Type без `;charset=utf-8` дает
    `invalid_json`» — заодно `Authorization: Bearer`, `token` в теле и куки
    `d=`/`b=` в одном вызове; `latest=true`+`inclusive=true` и `oldest` в params.

    Args:
        method: имя метода (`conversations.history`) — уйдёт в URL.
        token: сессионный токен (`xoxc-…`); пустой — запрос без токена.
        params: {'oldest': '1754640000.000000', 'latest': 'true', 'inclusive': 'true',
            'channel': 'CRLD5SBL4', 'limit': '200'} — уходят в query-строку.
        d_cookie, b_cookie, x_cookie, ss_cookie: значения кук сессии.
        base: базовый URL; '' — SLACK_API_BASE.
        token_in_body: имя ключа токена в JSON-теле; '' — токен только в заголовке.
        timeout: таймаут коннекта, сек.

    Returns:
        (parsed, None) — parsed dict|str (ok:false тоже в parsed, err None);
        либо (None, 'текст ошибки с телом ответа').
    """
    if not method or method.startswith('http'):
        return None, f'method — имя метода (conversations.history), а не URL: {method!r}'
    base = base or SLACK_API_BASE
    url = f'{base.rstrip("/")}/{method.strip("/")}'
    query = {key: str(value) for key, value in (params or {}).items()}
    if token and not token_in_body:
        query['token'] = token  # токен только в query: тот же «missing required field» паттерн
    url_full = url + ('?' + urllib.parse.urlencode(query) if query else '')
    body = {key: str(value) for key, value in (params or {}).items()}
    if token and token_in_body:
        body[token_in_body] = token
    headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20231130 '
                             'Firefox/121.0', 'Accept': 'application/json, text/plain, */*',
               'Accept-Language': 'en-US,en;q=0.5', 'Accept-Encoding': 'gzip, deflate',
               'DNT': '1', 'Sec-DNT': ':1', 'Origin': SLACK_WEB_BASE,
               'Referer': f'{SLACK_WEB_BASE}/', 'Sec-Fetch-Dest': 'empty',
               'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-SameSite': 'none',
               'Sec-Fetch-Store': 'network', 'Priority': 'u=0',
               'Accept-Domain': SLACK_WEB_BASE[8:], 'Connection': 'keep-alive',
               'Content-Type': 'application/json;charset=utf-8'}
    if token and token_in_body:
        headers['Authorization'] = f'Bearer {token}'
    pairs = [f'{name}={value}' for name, value in (('d', d_cookie), ('b', b_cookie),
                                                   ('x', x_cookie), ('ss', ss_cookie)) if value]
    if pairs:
        headers['Cookie'] = '; '.join(pairs)
    request = urllib.request.Request(url_full, data=(json.dumps(body).encode('utf-8')
                                                   if (body or token) else None),
                                     method='POST' if (body or token) else 'GET',
                                     headers=headers)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler,
                                             urllib.request.HTTPHandler)
        with opener.open(request, timeout=timeout) as response:
            payload = response.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as err:
        try:
            detail = err.read().decode('utf-8', 'ignore')[:240]
        except OSError:
            detail = str(err)[:240]
        return None, (f'{method} -> HTTP {err.code}: {detail} (частый признак: токен/куки '
                      f'не в теле и не в query — см. docstring)')
    except urllib.error.URLError as err:
        return None, (f'{method} -> не достать {url_full.split("?")[0]}: '
                      f'{getattr(err, "reason", err)}')
    except (http.client.HTTPException, OSError) as err:
        return None, f'{method} -> обрыв коннекта на {url_full.split("?")[0]}: {err}'
    return _slack_parse_payload(payload, method), None


# ── детали реализации ──


def _slack_ls_db_path(profile_dir: str, host: str) -> tuple:
    """Найти `ls/data.sqlite` хоста в профиле (storage/default, permanent, temporary)."""
    for root in ('storage/default', 'storage/permanent', 'storage/temporary'):
        path = os.path.join(profile_dir, root, host.replace('://', '+++'), 'ls', 'data.sqlite')
        if os.path.isfile(path):
            return path, None
    return None, (f'в профиле {profile_dir} нет хранилища localStorage '
                  f'(ожидался {SLACK_LS_DB_PATH})')


def _slack_scan_blob(tmp_db: str) -> tuple:
    """Сырой скан read-only копии localStorage: [(имя, полный токен), …] без дублей.

    Имя ключа — префикс токена (`xoxc-`, `d=`…); полные токены возвращаются
    наружу только через slack_extract_tokens(), в отпечаток — slack_token_prefix().
    """
    conn, err = sqllite3_connect_readonly(tmp_db)
    if conn is None:
        return None, f'read-only копия localStorage не читается ({tmp_db}): {err}'
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND lower(name)='data'")
        if not cur.fetchone():
            return [], f"в {tmp_db} нет таблицы 'data' (хранилище localStorage)"
        cur.execute('SELECT key, value FROM data')
        found = {}
        for _key, value in cur.fetchall():
            blob = value if isinstance(value, (bytes, bytearray)) \
                else str(value or '').encode('latin-1', 'ignore')
            for match in SLACK_SECRET_VALUE_RE.finditer(blob):
                secret = match.group('secret')
                head = secret.partition('=')[0] if '=' in secret[:3] else secret[:4]
                found.setdefault(head, secret)
        return list(found.items()), None
    except Exception as err:
        return None, f'read-only копия localStorage не читается ({tmp_db}): {err}'
    finally:
        conn.close()


def _slack_parse_payload(payload: str, method: str) -> object:
    """Разобрать ответ: JSON (ok:false — тоже данные, err None) либо html-глушилку."""
    if payload[:1] in ('{', '['):
        try:
            return json.loads(payload)
        except ValueError:
            pass
    if '<html' in payload[:200].lower():
        return {'ok': False, 'error': f'html вместо JSON на {method}: {payload[:200]}'}
    return {'raw': payload}


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('extract', 'token', 'call'), help='что показать')
    _P.add_argument('--profile', default=os.path.join(SLACK_TOKEN_DIR, 'profile'),
                    help=f'persistent-профиль (по умочалению {SLACK_TOKEN_DIR}/profile)')
    _P.add_argument('--host', default='app.slack.com', help='хост в пути хранилищ')
    _P.add_argument('--dir', default=SLACK_TOKEN_DIR,
                    help=f'каталог тикетницы (файл {SLACK_TOKEN_FILE}, 0600)')
    _P.add_argument('--method', default='', help='метод RPC для call')
    _P.add_argument('--params', nargs='*', default=None, metavar='КЛЮЧ=ЗНАЧЕНИЕ',
                    help='параметры в query-строку')
    _C = _P.parse_args()
    if _C.cmd in ('extract', 'token'):
        if _C.cmd == 'extract':
            _tokens, _err = slack_extract_tokens(_C.profile, host=_C.host, tmp_dir=_C.dir)
        else:
            _tokens, _err = slack_token_load(_C.dir)
        if not _tokens and _err:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _name in sorted(_tokens or {}):
            print(f'{_name:<6} = {_tokens[_name][:SLACK_TOKEN_PREFIX_LEN]!r}… '
                  f'({len(_tokens[_name])} симв.)')
    else:
        if not _C.method:
            print('ошибка: для call нужен --method', file=sys.stderr)
            raise SystemExit(1)
        _tokens, _err = slack_token_load(_C.dir)
        if _tokens is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        _params = dict(p.split('=', 1) for p in (_C.params or []))
        _body, _err = slack_api_call(_C.method, token=_tokens.get('xoxc', ''), params=_params,
                                     d_cookie=_tokens.get('d', ''), b_cookie=_tokens.get('b', ''),
                                     x_cookie=_tokens.get('x', ''), ss_cookie=_tokens.get('ss', ''),
                                     timeout=30)
        if _body is None or (isinstance(_body, dict) and _body.get('ok') is False):
            print(f'ошибка: {_err or _body}', file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(_body, ensure_ascii=False)[:4000])
    raise SystemExit(0)

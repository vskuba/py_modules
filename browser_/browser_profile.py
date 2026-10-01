"""
Форензика persistent-профиля Playwright/Firefox-браузера на диске: сессии, куки, localStorage.

«Человек залогинился в управляемом браузере → агент вытаскивает сессию из
профиля и далше ходит в сайт API-клиентом» — этот вход не покрыт
`browser_/browser_api.py` (тот ходит в Playwright API, а здесь читается профиль
с диска, когда контекст уже закрыт либо поднят вне пула через
`launchPersistentContext`).

Куки Firefox («cookies.sqlite», moz_cookies) и Chromium («Cookies»,
host_key) лежат в одном профиле, но разными файлами; localStorage Firefox —
в «data» BLOB'ах `storage/default/<origin>/ls/data.sqlite`.
"""
import os
import sqlite3

from sqllite3.sqllite3_ import sqllite3_connect_readonly, sqllite3_read_localstorage, \
    sqllite3_copy_readonly, sqllite3_scan_blob

# ── константы ──

BROWSER_PROFILE_TMP_DIR = '/tmp/.dsh-browser-profile'  # каталог read-only копий профилей
BROWSER_PROFILE_COOKIE_TABLES = (('moz_cookies', 'host'), ('cookies', 'host_key'))
BROWSER_PROFILE_SECRET_PREFIXES = ('xoxc-', 'xoxb-', 'xoxp-', 'xoxa-', 'xoxd-', 'xoxe-',
                                   'xoxs-', 'xoxr-', 'dc-', 'hc-', 'ct-', 'ss-', 'ssda-',
                                   'd=', 'b=', 'x=', 'ss=')  # префиксы сессионных токенов

# ── публичный API модуля ──


def browser_profile_cookies_db(profile_dir: str) -> str:
    """Найти в профиле файл кук (Firefox storage/.../cookies.sqlite, иначе Default/Cookies).

    Публичный: сессионные куки (`d`/`b`/`x`/`ss`) ищут и чаты web_/web_rpc.py, и
    slack_/slack_.py — локатор один, чтобы не плодить дубль по неймспейсамам."""
    for rel in ('storage/default/cookies.sqlite', 'cookies.sqlite', 'Default/Cookies',
                'Default/Network/Cookies', 'Network/Cookies'):
        candidate = os.path.join(profile_dir, rel)
        if os.path.isfile(candidate):
            return candidate
    return ''


def browser_profile_origins(profile_dir: str) -> tuple:
    """Перечислить хранилища профиля: origin'ы с localStorage и файл кук.

    Args:
        profile_dir: каталог persistent-профиля (то, что передали launchPersistentContext).

    Returns:
        ([{'origin': str, 'localstorage': str|None, 'cookies': str|None}], None)
        либо (None, 'текст ошибки').
    """
    if not os.path.isdir(profile_dir):
        return None, f'нет каталога профиля: {profile_dir}'
    cookies_db = browser_profile_cookies_db(profile_dir)
    rows = []
    for walk_root, dirs, files in os.walk(os.path.join(profile_dir, 'storage')):
        if 'data.sqlite' in files and os.path.basename(walk_root) == 'ls':
            rows.append({'origin': os.path.basename(os.path.dirname(walk_root)),
                         'localstorage': os.path.join(walk_root, 'data.sqlite'),
                         'cookies': cookies_db or None})
    if not rows:
        rows = [{'origin': '', 'localstorage': None, 'cookies': cookies_db or None}]
    if not any(r['localstorage'] or r['cookies'] for r in rows):
        return None, f'в профиле нет ни storage/default/*/ls/data.sqlite, ни cookies: {profile_dir}'
    return rows, None


def browser_profile_read_localstorage(ls_db_path: str, key_prefix: str = '') -> tuple:
    """Прочитать kv-карту localStorage из `ls/data.sqlite` (v10-BLOB или JSON-мап).

    Args:
        ls_db_path: путь к `data.sqlite`.
        key_prefix: оставить только ключи с этим началом ('xoxc-', 'ct-'); пусто — все.

    Returns:
        ({ключ: значение}, None) либо (None, 'текст ошибки').
    """
    kv, err = sqllite3_read_localstorage(ls_db_path)
    if err:
        return None, err
    if key_prefix:
        kv = {k: v for k, v in kv.items() if k.startswith(key_prefix)}
    return kv, None


def browser_profile_read_cookies(db_path: str, names: tuple = ('d', 'b', 'x', 'ss'),
                                 tmp_dir: str = BROWSER_PROFILE_TMP_DIR) -> tuple:
    """Вытащить куки из `cookies.sqlite` (moz_cookies) либо «Cookies» Chromium.

    Args:
        db_path: путь к sqlite-файлу с куками.
        names: имена кук; пусто — все.
        tmp_dir: каталог для read-only копий.

    Returns:
        ([{'name', 'host', 'value_prefix'}], None) — `value_prefix` обрезан до
        10 символов; либо (None, 'текст ошибки').
    """
    conn, err = sqllite3_connect_readonly(db_path, tmp_dir)
    if err:
        return None, err
    try:
        table, col_host = _browser_profile_cookie_schema(conn)
        sql = f'SELECT name, {col_host}, value FROM {table}'
        params = []
        if names:
            sql += f' WHERE name IN ({", ".join("?" for _ in names)})'
            params = list(names)
        rows = [{'name': r[0], 'host': r[1], 'value_prefix': (r[2] or '')[:10]}
                for r in conn.execute(sql, params).fetchall()]
        return rows, None
    except sqlite3.Error as err:
        return None, f'куки не прочитались ({db_path}): {err}'
    finally:
        conn.close()


def browser_profile_extract(profile_dir: str, host: str = 'app.slack.com',
                            prefixes: tuple = BROWSER_PROFILE_SECRET_PREFIXES,
                            value_prefix_len: int = 10) -> tuple:
    """Сырой байтовый скан профилейных хранилищ: найти сессионные токены по префиксам.

    «Key в Blob'е лежит raw байтами в начале Blob'а, typeof(value) врет 'text'
    (данные-то в Blob'е)» — JSON-разбор таких файлов не читает их, потому что
    Blob не JSON; здесь — сырой скан с обрезкой найденного до префикса.

    Args:
        profile_dir: каталог persistent-профиля.
        host: хост сайта в пути storage/default/https+++<host>; '' — все origin'ы.
        prefixes: искомые префиксы значений ('xoxc-', 'd='...).
        value_prefix_len: сколько байтов значения показывать в отчёте.

    Returns:
        ([{'file', 'table', 'key', 'offset', 'length', 'value_prefix'}], None) —
        полные токены наружу не кладут; либо (None, 'текст ошибки').
    """
    origins, err = browser_profile_origins(profile_dir)
    if err:
        return None, err
    targets = [r['localstorage'] for r in origins if r['localstorage']
               and (not host or host.replace('://', '+++') in (r['origin'] or ''))]
    targets += [r['cookies'] for r in origins if r['cookies']]
    found, seen = [], set()
    for path in dict.fromkeys(targets):
        if not path or path in seen:
            continue
        seen.add(path)
        for prefix in prefixes:
            rows, serr = sqllite3_scan_blob(path, prefix.encode('utf-8'), tmp_dir=BROWSER_PROFILE_TMP_DIR)
            if serr:
                return None, serr
            for row in rows:
                row['file'] = path
                row['value_prefix'] = row['value_prefix'][:value_prefix_len]
                found.append(row)
    return found, None


def browser_profile_copy_readonly(db_path: str, tmp_dir: str = BROWSER_PROFILE_TMP_DIR) -> tuple:
    """Собрать read-only копию БД чужого приложения (прокидывает tmp; мост к sqllite3_).

    Args:
        db_path: путь к исходному (возможно, заблокированному) файлу.
        tmp_dir: каталог для копий.

    Returns:
        (путь копии, None) либо (None, 'текст ошибки').
    """
    return sqllite3_copy_readonly(db_path, tmp_dir)



# ── детали реализации ──


def _browser_profile_cookie_schema(conn: sqlite3.Connection) -> tuple:
    """Угадать схему кук: moz_cookies(host) у Firefox, cookies(host_key) у Chromium."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table, col_host in BROWSER_PROFILE_COOKIE_TABLES:
        if table in tables:
            return table, col_host
    return 'moz_cookies', 'host'


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('--profile', required=True, help='каталог persistent-профиля')
    _P.add_argument('--host', default='app.slack.com', help='хост сайта в storage (пусто — все)')
    _P.add_argument('--extract', action='store_true', help='сырой скан токенов вместо листка')
    _C = _P.parse_args()
    if _C.extract:
        _rows, _err = browser_profile_extract(_C.profile, _C.host)
        if _err:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _r in _rows:
            print(f"{os.path.relpath(_r['file'], _C.profile)} [{_r['key'][:48]}] "
                  f"offset={_r['offset']} len={_r['length']} value={_r['value_prefix']!r}")
    else:
        _rows, _err = browser_profile_origins(_C.profile)
        if _err:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        for _r in _rows:
            print(f"{_r['origin']:<40} ls={_r['localstorage'] or '-'} cookies={_r['cookies'] or '-'}")
    raise SystemExit(0)
"""
Рабочий с sqlite-файлами чужих приложений: read-only открытие заблокированных БД, raw-скан BLOB.

SQLite не даёт читать файл БД, который приложение держит открытым на запись, а
`file:...?mode=ro` на таком файле дает «attempt to write a readonly database»:
движок для read-only режима все равно требует прав на запись. Вход один: снять
копию в tmp и открыть копию — отсюда и `immutable` на копии: с неё никто не пишет.

Рядом лежит разбор BLOB'ов localStorage Firefox («data» с типом 2: `key` — raw
байты в начале Blob'а, и `typeof(value)` врет 'text'/'blob' от сборки к сборке) —
тот же механизм читают `browser_/browser_profile.py` и `slack_/slack_.py`.
"""
import hashlib
import json
import os
import shutil
import sqlite3

# ── константы ──

SQLLITE3_TMP_DIR = '/tmp/.dsh-sqlite'  # каталог read-only копий; не в /tmp без прав 0700

# ── публичный API модуля ──


def sqllite3_connect_readonly(db_path: str, tmp_dir: str = SQLLITE3_TMP_DIR) -> tuple:
    """Открыть чужую БД read-only: копию в tmp, открытую с `mode=ro`.

    Args:
        db_path: путь к файлу БД чужого приложения.
        tmp_dir: каталог для копий; создается при надобности.

    Returns:
        (connection, None) либо (None, 'текст ошибки').
    """
    tmp_path, err = sqllite3_copy_readonly(db_path, tmp_dir)
    if err:
        return None, err
    try:
        conn = sqlite3.connect(f'file:{tmp_path}?mode=ro', uri=True)
        return conn, None
    except sqlite3.Error as err:
        return None, f'read-only копия не открылась ({tmp_path}): {err}'


def sqllite3_copy_readonly(db_path: str, tmp_dir: str = SQLLITE3_TMP_DIR) -> tuple:
    """Собрать read-only копию БД в tmp и вернуть путь к ней.

    Копия снимается один раз и служит всем последующим чтениям: файл БД чужого
    приложения заблокирован на запись, а `mode=ro` с прямой файл не спасает.

    Args:
        db_path: путь к исходному (возможно, заблокированному) файлу.
        tmp_dir: каталог для копий.

    Returns:
        (путь копии, None) либо (None, 'текст ошибки').
    """
    if not os.path.isfile(db_path):
        return None, f'нет файла БД: {db_path}'
    if not os.path.isdir(tmp_dir):
        try:
            os.makedirs(tmp_dir, exist_ok=True)
            os.chmod(tmp_dir, 0o700)
        except OSError as err:
            return None, f'tmp-каталог не собрался ({tmp_dir}): {err}'
    tmp_path = os.path.join(tmp_dir, os.path.basename(db_path) + '.' +
                            hashlib.sha256(db_path.encode('utf-8')).hexdigest()[:12] + '.ro')
    if not os.path.isfile(tmp_path):
        staged = tmp_path + '.part'
        try:
            shutil.copy2(db_path, staged)
            os.chmod(staged, 0o600)
            os.replace(staged, tmp_path)
        except OSError as err:
            try:
                os.unlink(staged)
            except OSError:
                pass
            return None, f'копия не снялась: {err}'
    return tmp_path, None


def sqllite3_kv_extract(blob) -> tuple:
    """Вытащить пары «ключ → значение» из BLOB'а localStorage (v10-формат Firefox).

    Формат записи: `\\x01 ключ \\x00 значение \\x00 expiration:f64le lastAccess:f64le`
    подряд, где `key` — raw байты в начале Blob'а, и `typeof(value)` врет
    'text'/'blob' от сборки к сборке — поэтому скан сырыми байтами.

    Args:
        blob: байты из колонки (bytes/bytearray/memoryview) либо None.

    Returns:
        (dict, None) либо (None, 'текст ошибки').
    """
    if blob is None:
        return None, 'BLOB пуст (NULL): колоноку с данными не нашли?'
    data = bytes(blob) if not isinstance(blob, (bytes, bytearray, memoryview)) else blob
    out = {}
    pos, size = 0, len(data)
    while pos + 1 < size and data[pos] == 0x01:
        nul = data.find(b'\x00', pos + 1)
        if nul < 0:
            break
        key = data[pos + 1:nul].decode('utf-8', 'ignore')
        pos = nul + 1
        nul = data.find(b'\x00', pos)
        if nul < 0:
            break
        value = data[pos:nul].decode('utf-8', 'ignore')
        pos = nul + 1
        out[key] = value
        pos += 16  # expiration и lastAccess — два f64 LE
    return out, None


def sqllite3_read_localstorage(db_path: str) -> tuple:
    """Прочитать localStorage из `storage/default/*/ls/data.sqlite` профиля Firefox.

    Ключи в «data» BLOB'а лежат raw байтами в начале Blob'а; `typeof()` врет,
    поэтому строки с типом blob и text разбираются одинаково — v10-разбором.

    Args:
        db_path: путь к `data.sqlite` внутри профиля.

    Returns:
        ({ключ: значение}, None) либо (None, 'текст ошибки').
    """
    conn, err = sqllite3_connect_readonly(db_path)
    if err:
        return None, err
    try:
        out = {}
        for row in conn.execute('SELECT key, typeof(value), value FROM data').fetchall():
            if row[1] == 'blob':
                kv = sqllite3_kv_extract(row[2])[0]
                if kv:
                    out.update(kv)
                else:
                    out[row[0]] = _sqllite3_blob_key(row[2])
            elif row[1] == 'text':
                out.update(_sqllite3_json_map(row[2] or ''))
            else:
                out[row[0]] = '' if row[2] is None else str(row[2])
        return out, None
    except sqlite3.Error as err:
        return None, f'localStorage не прочитался: {err}'
    finally:
        conn.close()


def sqllite3_scan_blob(db_path: str, pattern: bytes, column: str = 'value',
                       table: str = '', limit: int = 20,
                       tmp_dir: str = SQLLITE3_TMP_DIR) -> tuple:
    """Найти подстроку в BLOB'ах таблицы, не расшифровывая формат: raw-скан `instr()`.

    «Key в Blob'е лежит raw байтами в начале Blob'а, typeof(value) врет 'text'»
    — `LIKE` по BLOB-колонке плывет от сборки к сборке, `instr(BLOB, x'..')` —
    нет: сравнивает байты как есть, и JSON-разбор не нужен, потому что Blob не JSON.

    Args:
        db_path: путь к БД (копии).
        pattern: байты для поиска (b'xoxc-', b'd='...).
        column: имя BLOB-колонки; пусто — искать по всем колокам BLOB.
        table: имя таблицы; пусто — искать по всем таблицам с BLOB-колонкой.
        limit: сколько находок вернуть.
        tmp_dir: каталог для read-only копий.

    Returns:
        ([{'table', 'key', 'offset', 'length', 'value_prefix'}], None) —
        `value_prefix` обрезан до 10 байтов: дампы с токенами не плодят секреты.
    """
    conn, err = sqllite3_connect_readonly(db_path, tmp_dir)
    if err:
        return None, err
    try:
        if table:
            tables = [table]
        else:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND sql LIKE '%BLOB%' COLLATE NOCASE").fetchall()]
        rows, found = [], 0
        for table_name in tables:
            columns = ([column] if column else
                       [c[1] for c in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                        if (c[2] or '').upper() == 'BLOB'])
            for column_name in columns:
                sql = (f'SELECT rowid, "{column_name}", length("{column_name}") '
                       f'FROM "{table_name}" WHERE instr("{column_name}", ?) > 0 LIMIT ?')
                for row in conn.execute(sql, (bytes(pattern), limit)).fetchall():
                    blob = row[1] if isinstance(row[1], (bytes, bytearray)) else b''
                    rows.append({'table': table_name, 'key': _sqllite3_blob_key(blob),
                                 'offset': bytes(blob).find(bytes(pattern)) if blob else -1,
                                 'length': row[2],
                                 'value_prefix': bytes(blob)[:10].decode('utf-8', 'ignore')})
                    found += 1
                    if found >= limit:
                        return rows, None
        return rows, None
    except sqlite3.Error as err:
        return None, f'raw-скан BLOB не удался: {err}'
    finally:
        conn.close()



# ── детали реализации ──


def _sqllite3_blob_key(blob) -> str:
    """Ключ записи v10: raw байты после первого 0x01-байта до первого 0x00."""
    try:
        raw = bytes(blob) if blob else b''
    except Exception:
        return ''
    start = raw.find(b'\x01')
    if start < 0:
        return ''
    end = raw.find(b'\x00', start + 1)
    if end < 0:
        end = len(raw)
    return raw[start + 1:end].decode('utf-8', 'ignore')


def _sqllite3_json_map(text: str) -> dict:
    """Разобрать JSON-мап из value (старые сборки Firefox клали JSON прямо в BLOB)."""
    try:
        return {str(k): str(v) for k, v in json.loads(text).items()}
    except (ValueError, TypeError, AttributeError):
        return {}


if __name__ == '__main__':  # py-codex-hist load-line: правь shell-строку, не трогай код выше
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('--db', help='файл БД чужого приложения')
    _P.add_argument('--grep', help='сырая подстрока для raw-скана BLOB, напр. b"xoxc-"')
    _P.add_argument('--table', default='', help='таблица; пусто — все с BLOB-колонокой')
    _P.add_argument('--column', default='value', help='BLOB-колока')
    _P.add_argument('--kv', action='store_true', help='показать kv из localStorage-BLOB')
    _C = _P.parse_args()
    if not _C.db:
        raise SystemExit('ошибка: укажи --db с путём к БД')
    if not _C.kv and not _C.grep:
        raise SystemExit('ошибка: для скана укажи --grep, для kv — --kv')
    if _C.kv:
        _kv, _err = sqllite3_read_localstorage(_C.db)
        if _err:
            raise SystemExit(f'ошибка: {_err}')
        for _k, _v in sorted(_kv.items()):
            print(f'{_k[:64]:<64} = {_v[:10]}… ({len(_v)}б)')
    else:
        _rows, _err = sqllite3_scan_blob(_C.db, _C.grep.encode('utf-8', 'ignore'),
                                         _C.column, _C.table)
        if _err:
            raise SystemExit(f'ошибка: {_err}')
        if not _rows:
            raise SystemExit(f'ошибка: в {_C.table or "всех BLOB-колоках"} нет {_C.grep!r}')
        for _r in _rows:
            print(f"{_r['table']}.{_r['key'][:48]:<48} offset={_r['offset']} "
                  f"len={_r['length']} value={_r['value_prefix']!r}")
    raise SystemExit(0)

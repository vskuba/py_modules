"""Реестр сделанного по ключу: журнал jsonl, последняя строка — лицо ключа.

Публикации на чужих площадках и заведённые аккаунты требуют памяти: после
любого обрыва состояние «опубликовано / на модерации / отбито» восстанавливается
по логам наугад — а по реестру сверяется за минуту. Ключ — строка на выбор
зующего (`vidhuky.com`, `marker.org.ua#review-123`); поля — словарь, какие
угодно: площадка сама диктует, чем она отчитывается.

Журнал **append-only**: правок строк нет, каждое изменение — новая строка с
полным лицом ключа (старое лицо + свежие поля). Цена — файл растёт; выгода —
полужурнал читается до конца, оборванная последняя строка не прячет прошлые.

## Что здесь есть

* `web_ledger_put` — upsert ключа: последняя его строка + новые поля;
* `web_ledger_get` — лицо ключа (или весь журнал списком, `all_rows`).
"""
import json
import sys
from datetime import datetime, timezone


def web_ledger_put(path: str, key: str, fields: dict | None = None) -> dict:
    """Записать ключ в журнал: лицом = прежнее лицо + свежие поля.

    Args:
        path: файл журнала (`.jsonl`); отсутствует — создастся.
        key: ключ записи, на усмотрение зующего.
        fields: свежие поля; `None`/пусто — просто повтор последнего лица.

    Returns:
        {'ok': True, 'row': записанное лицо (с key/seq/at)} или
        {'ok': False, 'row': {}, 'error': словами}
    """
    face = web_ledger_get(path, key=key)
    if not face['ok']:
        return face
    row = dict(face['rows'][0]) if face['rows'] else {}
    row.pop('seq', None)
    row.pop('at', None)
    row.update(fields or {})
    row['key'] = key
    row['seq'] = (face['rows'][0]['seq'] + 1) if face['rows'] else 1
    row['at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        return {'ok': True, 'row': row, 'error': ''}
    except OSError as e:
        return {'ok': False, 'row': {}, 'error': str(e)}


def web_ledger_get(path: str, key: str = '', all_rows: bool = False) -> dict:
    """Прочитать журнал: лицо каждого ключа (или лица всех строк, all_rows).

    Args:
        path: файл журнала; нет файла — пустой список, не отказ.
        key: только этот ключ; пусто — все ключи.
        all_rows: True — весь журнал от старых к новым, без свёртки по ключам.

    Returns:
        {'ok': True, 'rows': [...]}, при отказе — {'ok': False, 'rows': [],
        'error': словами}. Оборванная строка в конце журнала пропускается
        молча: её половина всё равно не является лицом.
    """
    try:
        with open(path, encoding='utf-8') as f:
            raw = f.read().splitlines()
    except FileNotFoundError:
        return {'ok': True, 'rows': [], 'error': ''}
    except OSError as e:
        return {'ok': False, 'rows': [], 'error': str(e)}
    rows: list = []
    for line in raw:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and 'key' in row:
            rows.append(row)
    if all_rows:
        out = rows if not key else [r for r in rows if r['key'] == key]
        return {'ok': True, 'rows': out, 'error': ''}
    last: dict = {}
    for row in rows:
        last[row['key']] = row
    out = list(last.values())
    if key:
        out = [last[key]] if key in last else []
    return {'ok': True, 'rows': out, 'error': ''}


if __name__ == '__main__':
    a = sys.argv[1:]
    if len(a) < 2:
        print('употребление: python -m web_.web_ledger put ФАЙЛ КЛЮЧ [\'{"поле":"значение"}\']')
        print('               python -m web_.web_ledger get ФАЙЛ [КЛЮЧ] [--all]')
        raise SystemExit(2)
    if a[0] == 'put':
        body = json.loads(a[3]) if len(a) > 3 else {}
        print(web_ledger_put(a[1], a[2], body))
    else:
        print(web_ledger_get(a[1], key=a[2] if len(a) > 2 and a[2] != '--all' else '',
                             all_rows='--all' in a))

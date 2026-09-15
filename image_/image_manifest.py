"""
Паспорт батча при рождении: каждый создаваемый кадр сам дописывает, откуда он.

«Восстанови вчерашнюю формулу» стоило часа раскопок git log и /tmp: имена
файлов молчат, seed/workflow/box помнились только в голове писавшего. Модуль
закрывает дыру с обеих сторон: при создании файла строка с временем, батчем,
seed, workflow и указателями ложится в соседний manifest.json, а «откуда этот
файл» — один запрос по хэшу, имени или временному окну.

Читает и чужие манифеды без переписывания: список строк (photos) и словарь
«1»..«16» (source) — обычные формы, правится то, что уже лежит.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = 'manifest.json'
IMAGE_MANIFEST_SHA = 8   # первых хэшей достаточно: имя+хэш различают кадры батча


def _sha(path, n=IMAGE_MANIFEST_SHA):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:n]


def _read(target):
    """Манифест как список строк: dict-форма («1»..«16») сворачивается в строки."""
    p = Path(target)
    man = p if p.name == MANIFEST else p / MANIFEST
    if not man.exists():
        return [], man
    raw = json.loads(man.read_text())
    return (list(raw.values()) if isinstance(raw, dict) else raw), man


def image_manifest_save(files, dest, **origin):
    """Дописать/обновить строки паспортов; вернуть их. Файл правится на месте.

    Строка: {file, t (UTC, ISO), sha} плюс всё, что передавший зовёт ключами
    (batch, seed, workflow, box, kps …). Существующая строка с тем же именем
    обновляется полями зовущего, а не удваивается.
    """
    rows, man = _read(dest)
    by_name = {r.get('file'): r for r in rows}
    done = []
    for f in map(Path, files):
        rec = by_name.get(f.name)
        if rec is None:
            rec = {'file': f.name}
            rows.append(rec)
        rec.update({'t': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'sha': _sha(f), **origin})
        done.append(rec)
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + '\n')
    return done


def image_manifest_find(target, name='', sha='', since='', until='', **fields):
    """Строки-паспорта по имени, хэшу, времени (ISO) или полям origin; молчит пусто."""
    rows, _ = _read(target)
    out = []
    for r in rows:
        if name and Path(str(r.get('file', ''))).stem != Path(name).stem:
            continue
        if sha and not str(r.get('sha', '')).startswith(sha):
            continue
        t = str(r.get('t', ''))
        if since and t and t < since:
            continue
        if until and t and t > until:
            continue
        if any(r.get(k) != v for k, v in fields.items()):
            continue
        out.append(r)
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='image_manifest',
                                 description='паспорт батча: сохранить/найти')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('save', help='дописать строки при создании файлов')
    s.add_argument('files', nargs='+')
    s.add_argument('--dest', required=True, help='каталог или манифест')
    s.add_argument('--origin', default='{}', help='JSON полей passportа')
    f_ = sub.add_parser('find', help='откуда файл: хэш/имя/окно/поля')
    f_.add_argument('target', help='каталог с manifest.json или сам манифест')
    f_.add_argument('--name', default='')
    f_.add_argument('--sha', default='')
    f_.add_argument('--since', default='')
    f_.add_argument('--until', default='')
    ns = ap.parse_args()
    if ns.cmd == 'save':
        got = image_manifest_save(ns.files, ns.dest, **json.loads(ns.origin))
    else:
        got = image_manifest_find(ns.target, name=ns.name, sha=ns.sha,
                                 since=ns.since, until=ns.until)
    print(json.dumps(got, ensure_ascii=False, indent=1))

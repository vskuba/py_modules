"""
Приёмка миграций одной командой: порядок, страховки, латиница.

Файл миграции читает yoyo по порядку имён, а человек — глазами, и глазами не
видно, что ALTER с меткой раньше CREATE-файла лезет в таблицу, которой на свежей
базе ещё нет (панель не стартует), что файл без information_schema-страховки
вторым прогоном сломает данные, а смена метки уже применённого файла значит
тихий перепрогон. Функция открывает каталог `migrations/` так, как его видит
yoyo, и отвечает списком находок; ничего не исполняет — только читает.

Грабли, из-за которых это не grep:
* порядок наката — лексикографический по именам файлов, сверять надо его, а не
  дату в голове автора;
* `_yoyo_migration` хранит migration_id применённого файла: переименованный
  (перемеченный) файл — для yoyo новый, он применится второй раз;
* некавытированный идентификатор MySQL принимает U+0080–U+00FF, кириллица
  (U+0400+) вне диапазона — `AS этап` в теле запроса есть синтаксическая
  ошибка; в комментариях `--` и в `COMMENT '...'` кириллица, напротив, норма.
"""
import argparse
import asyncio
import re
from pathlib import Path

_CREATE = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?',
                     re.I)
_ALTER = re.compile(r'ALTER\s+TABLE\s+`?(\w+)`?', re.I)
_GUARD = re.compile(r'IF\s+NOT\s+EXISTS|INFORMATION_SCHEMA', re.I)
_MUTATES = re.compile(r'\b(?:ADD|DROP|MODIFY|CHANGE)\s+COLUMN|CREATE\s+TABLE',
                     re.I)
# Метка этапа в отчёте миграции. ⚠⚠ Русские слова наравне с английскими: проекты
# пишут `SELECT 'ДО' AS этап`, и, зная только `before`/`after`, проверка объявляла
# «нет маркеров этапов» у 78 миграций из 78 — то есть у всех до единой. Проверка,
# срабатывающая всегда, не сообщает ничего.
_STAGE = re.compile(r"'(before|after|ДО|ПОСЛЕ)'\s+AS\s+`?\w+", re.I | re.U)
_LABEL = re.compile(r'^(\d{14})_')
_COMMENT_BLOCK = re.compile(r'/\*.*?\*/', re.S)
# COMMENT-литерал целиком (обычный '...' и удвоенный ''...'' внутри @sql,
# с `=` и без): его содержимое — комментарий колонки, кириллица там законна.
_COMMENT_LITERAL = re.compile(
    r"COMMENT\s*=?\s*''(?:''|[^'])*?''|COMMENT\s*=?\s*'[^']*'", re.I)


def mysql_migration_check_check(migrations_dir, applied_ids=()) -> list[dict]:
    """Каталог миграций глазами yoyo: список находок {file, kind, detail}.

    Args:
        migrations_dir: каталог с *.sql (обходятся в порядке имён).
        applied_ids: применённые id из `_yoyo_migration`; пусто — сверка
            порядка без состояния базы.

    Returns:
        [{'file', 'kind', 'detail'}, ...]; пусто — каталог чист.
    """
    files = sorted(Path(migrations_dir).glob('*.sql'))
    order = [f.name for f in files]
    texts = {f.name: f.read_text(encoding='utf-8') for f in files}
    created: dict[str, str] = {}
    for name in order:
        for t in _CREATE.findall(texts[name]):
            created.setdefault(t.lower(), name)

    findings: list[dict] = []

    def note(fname, kind, detail=''):
        findings.append({'file': fname, 'kind': kind, 'detail': detail})

    for name in order:
        raw = texts[name]
        masked = _masked_lines(raw)
        for t in {x.lower() for x in _ALTER.findall(raw)}:
            if t in created and order.index(created[t]) > order.index(name):
                note(name, 'alter-before-create',
                     f'таблица `{t}` создаётся только {created[t]}')
        if any(_MUTATES.search(l.split('--')[0]) for l in masked) \
                and not any(_GUARD.search(l.split('--')[0]) for l in masked):
            note(name, 'unguarded', 'DDL без information_schema-страховки')
        body = '\n'.join(l.split('--')[0] for l in masked)
        if len(_STAGE.findall(body)) < 2:
            note(name, 'no-stage-markers', "нет пары SELECT 'before'/'after'")
        for no, line in enumerate(masked, 1):
            bad = sorted({c for c in line.split('--')[0] if ord(c) > 127})
            if bad:
                note(name, 'non-latin-in-query',
                     f'стр. {no}: ' + ''.join(bad))
    labels: dict[str, list[str]] = {}
    for name in order:
        if m := _LABEL.match(name):
            labels.setdefault(m.group(1), []).append(name)
    for names in labels.values():
        for n in names[1:]:
            note(n, 'shared-label', f'метка {n[:14]} ещё у {names[0]}')

    applied = set(applied_ids or ())
    if applied:
        for name in order:
            if name.rsplit('.', 1)[0] not in applied:
                note(name, 're-runs-on-start',
                     'база не помнит этот id — накатится вновь')
        for a in sorted(applied - {n.rsplit('.', 1)[0] for n in order}):
            findings.append({'file': a, 'kind': 'applied-not-on-disk',
                             'detail': 'база помнит id, которого нет здесь'})
    return findings


def _masked_lines(raw: str) -> list[str]:
    """Строки файла, где block-комментарии и COMMENT-литералы сбиты в X
    (номера строк целы): что осталось не-latin'ицей — та правда в запросе."""
    out = _COMMENT_LITERAL.sub(lambda m: ''.join(
        '\n' if c == '\n' else 'X' for c in m.group(0)), raw)
    out = _COMMENT_BLOCK.sub(lambda m: ''.join(
        '\n' if c == '\n' else 'X' for c in m.group(0)), out)
    return out.splitlines()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Приёмка каталога migrations: порядок наката, страховки, '
                    'латиница, перепрогон. Ничего не исполняет.')
    ap.add_argument('dir', nargs='?', default='migrations')
    ap.add_argument('--db', action='store_true',
                    help='сверить с _yoyo_migration живой базы (из .env)')
    ns = ap.parse_args()

    async def _applied() -> list[str]:
        """Применённые id из `_yoyo_migration`: адрес из .env, а когда он
       内容器ский — порт, опубликованны наружу контейнером с mysql."""
        from urllib.parse import urlparse
        import subprocess
        import pymysql
        from mysql_.mysql_ import mysql_get_url
        from urllib.parse import unquote
        p = urlparse(mysql_get_url())
        host, port = p.hostname, int(p.port or 3306)
        if not _reachable(host, port):
            got = _published_mysql(port)
            if got:
                host, port = got
        conn = pymysql.connect(host=host, port=port, user=p.username,
                               password=unquote(p.password or ''),
                               db=p.path.lstrip('/'))
        with conn.cursor() as cur:
            cur.execute('SELECT migration_id FROM _yoyo_migration')
            ids = [r[0] for r in cur.fetchall()]
        conn.close()
        return ids

    def _reachable(host: str, port: int) -> bool:
        import socket
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            return False

    def _published_mysql(container_port: int):
        import subprocess
        done = subprocess.run(
            ['docker', 'ps', '--format', '{{.Names}}\t{{.Ports}}'],
            capture_output=True, text=True)
        for line in (done.stdout or '').splitlines():
            parts = line.split('\t')
            if len(parts) == 2 and 'mysql' in parts[0]:
                m = re.search(r'[^,]*?:(\d+)->%d/tcp' % container_port,
                              parts[1])
                if m:
                    return '127.0.0.1', int(m.group(1))
        return ()

    try:
        ids = asyncio.run(_applied()) if ns.db else ()
        found = mysql_migration_check_check(ns.dir, ids)
        for item in found:
            print(f"{item['kind']:22} {item['file']:46} {item['detail']}")
        print(f'находок: {len(found)}')
    except Exception as err:
        raise SystemExit(f'ошибка: {err}')

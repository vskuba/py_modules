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
        # ⚠⚠ «Стоп»-находка: склеенная миграция не применится вовсе, а приложение
        # упадёт на старте вместе с ней. Стоит до прочих — остальные про
        # оформление, эта про то, доедет ли файл.
        if merged := mysql_migration_split_merged(raw):
            note(name, 'merged-statements', merged)

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


def mysql_migration_split_merged(raw: str) -> str:
    """Склеит ли yoyo запросы этого файла. Пусто — нарежется как написано.

    Args:
        raw: текст файла миграции.

    Returns:
        str: словами, сколько запросов потеряется. Пусто — всё в порядке (или
        `sqlparse` не поставлен — тогда ответа нет вовсе, см. ⚠ ниже).

    ## ⚠⚠ Зачем: файл применяется НЕ так, как его прочитал человек

    yoyo режет файл на запросы не сам, а `sqlparse.split()` — и тот на некоторых
    выражениях теряет границу. Тогда два запроса уезжают в базу **одной** строкой,
    MySQL отвечает `1064 … near 'UPDATE …'`, миграция не применяется, а стартующее
    приложение падает вместе с ней.

    ⚠ Измерено на живой беде: файл из восьми запросов `sqlparse` 0.5.5 нарезал на
    **два**. Прод-выкладка упала, blue-green откатил слот.

    ## ⚠⚠⚠ Почему это не ловится ни глазами, ни репетицией

    Глазами — потому что файл синтаксически безупречен: точки с запятой на месте.
    Репетицией (`mysql_rehearse_run`) — потому что она гоняет файл **клиентом
    `mysql`**, а у того свой разборщик, и он то же самое принимает молча. То есть
    единственная проверка миграции шла путём, которым прод не ходит.

    ## ⚠⚠ Считается РАЗНИЦА, и только в одну сторону

    Сверяем, сколько запросов вышло у `sqlparse`, со счётом точек с запятой в
    конце строк кода — то есть с тем, как файл читает человек.

    **Меньше** — склейка: запрос не доедет. **Больше** — не беда: `sqlparse`
    иногда дробит мельче (два запроса в строке, `DELIMITER`), и каждый кусок при
    этом остаётся годным.

    ⚠ Направление измерено, а не угадано: на 295 файлах двух проектов «больше»
    встретилось 27 раз, «меньше» — ни разу, а сломанный файл ловится. Проверка,
    ругающаяся на 27 живых миграций, была бы отключена в первый же день.

    ⚠⚠ Виновата не «обратная косая» как таковая: `LIKE '10\\_x%'` режется верно, а
    `REPLACE(REPLACE(x,'\\\\','\\\\\\\\'),'a','b')` — уже нет. Разница во
    **вложенности** вызова, а её автор запроса отслеживать не обязан. Потому и
    проверяется исход, а не стиль: спрашиваем сам `sqlparse`, что у него вышло.
    """
    try:
        import sqlparse
    except ImportError:
        # ⚠ Нет разборщика — нет и ответа. Соврать «чисто» здесь хуже, чем не
        # сказать ничего, но и валить приёмку из-за необязательной зависимости
        # незачем: `sqlparse` приходит с yoyo, и где есть yoyo, есть и он.
        return ''

    body = _COMMENT_BLOCK.sub(' ', raw)
    wanted = sum(1 for line in body.splitlines()
                 if line.split('--')[0].rstrip().endswith(';'))
    got = len(sqlparse.split(raw))

    if got < wanted:
        return (f'yoyo нарежет {got} запросов вместо {wanted}: часть уедет '
                f'одной строкой и не применится')

    return ''


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

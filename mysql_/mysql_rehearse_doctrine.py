"""
Репетиция Doctrine-миграций: одноразовая MySQL, up → prev → up, счётчики строк и вердикт идемпотентности.

У yoyo-вкуса `mysql_rehearse` сравнивать выводы второго наката — у PHP-сервисов на Doctrine
migations «второго наката» нет: там есть **откат** (`migrate prev`), и кривой `down()`
(не дописал удаление строки, не вернул индекс) видно только на нём. Ручной круг — свежий
контейнер, сеть, `DATABASE_URL`, разбор `COUNT(*)` руками: двадцать минут, и каждый раз
страшно задеть живую базу.

Здесь круг целиком:

 - приватная docker-сеть и одноразовый mysql-контейнер (имена со случайным суффиксом,
   снимаются в любом исходе, включая исключение); живая база не поднимается и не
   опрашивается — `DATABASE_URL` указывает только в репетиционную;
 - PHP-образ репозитория над кодом worktree (те же монтирования, что у сюит, —
   `project_worktree_php_run`: код из копии, `vendor/` из основной на read-only);
 - `bin/console doctrine:migrations:migrate` на свежей базе — полная цепочка,
   `migrate prev` × prev — откат свежих миграций, `migrate` снова — повторный накат;
 - между фазами — число строк таблиц, которых касаются откатываемые миграции (имена
   разбираются из текста миграций), и результат `--sql-check`, если задан.

⚠ Таблицы **разбираются из текста**, а не спрашиваются у базы: в PHP-миграции они
сидят внутри `addSql('INSERT INTO …')`. Прочитать умею `INSERT [IGNORE] INTO`,
`REPLACE INTO`, `UPDATE`, `DELETE FROM`, `CREATE/ALTER/DROP/RENAME/TRUNCATE TABLE`,
`CREATE/DROP INDEX … ON`; имя, собранное конкатенацией в цикле, не разберётся —
в отчёте такая таблица покажет `—`, и это честное «не знаю», а не ноль.

⚠ Идемпотентным up→down→up считается, когда **числа строк второго `up` равны числам
первого** (и `--sql-check` совпал). Выводы сравнивать бессмысленно: doctrine печатает
одно и то же, делая разное.
"""
import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mysql_.mysql_source import mysql_source_ask
from project_.project_worktree_php import project_worktree_php_run

MYSQL_REHEARSE_DOCTRINE_IMAGE = 'mysql:8.0'      # образ одноразовой базы; --image перебивает
MYSQL_REHEARSE_DOCTRINE_DB = 'rehearse'           # имя базы внутри контейнера
MYSQL_REHEARSE_DOCTRINE_READY_TIMEOUT = 240       # секунд на «созрел» mysql-контейнер
MYSQL_REHEARSE_DOCTRINE_PHASE_TIMEOUT = 1800      # секунд на фазу console-прогона

# Ключевые слова, за которыми стоит имя таблицы; длинные формы раньше коротких.
_TABLE_IN = re.compile(
    r'\b(?:INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|DELETE\s+FROM|UPDATE|'
    r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS|CREATE\s+TABLE|DROP\s+TABLE\s+IF\s+EXISTS|DROP\s+TABLE|'
    r'ALTER\s+TABLE|TRUNCATE\s+TABLE|TRUNCATE|RENAME\s+TABLE|'
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+[`\w.]+\s+ON|DROP\s+INDEX\s+[`\w.]+\s+ON)'
    r'\s+`?([A-Za-z0-9_]+)`?', re.I)

# Стоило один раз поймать ложные таблицы: `ON DUPLICATE KEY UPDATE col=…` и
# `ON UPDATE CURRENT_TIMESTAMP` — не оператор UPDATE, а атрибуты; префикс решает.
_NOT_UPDATE = re.compile(r'(?:\bON\b|DUPLICATE\s+KEY)\s*$', re.I)
_NAME_OK = re.compile(r'[A-Za-z0-9_]+')
_JUNK = {'IF', 'EXISTS', 'NOT', 'SELECT', 'SET', 'WHERE', 'TABLE', 'IGNORE'}


def mysql_rehearse_doctrine_run(repo: str, worktree: str = '', image: str = MYSQL_REHEARSE_DOCTRINE_IMAGE,
                                prev: int = 1, sql_check: str = '', timeout: float = MYSQL_REHEARSE_DOCTRINE_PHASE_TIMEOUT) -> dict:
    """Накатить миграции на одноразовой MySQL, откатить prev штук, накатить снова; посчитать строки и сказать вердикт.

    Args:
        repo: основная выкладка PHP-проекта (от неё берутся образ и `vendor/`).
        worktree: копия, чей код крутится; пусто — сам `repo`.
        image: образ mysql для репетиционной базы.
        prev: сколько раз откатить `migrate prev` (по умолчанию 1 — последняя миграция).
        sql_check: запрос, чей ответ сверяется между фазами (например `SELECT COUNT(*) …`).
        timeout: секунд на фазу console-прогона.

    Returns:
        `{'ok', 'idempotent', 'digest', 'log', 'tables', 'migrations', 'phases'}` —
        дайджест словами, путь к полному журналу, разбираемые таблицы, файлы миграций
        и пометки фаз `{label, codes, ok, counts, check, delta, tail}`.

    Raises:
        RuntimeError: подготовка базы не удалась; контейнер и сеть при этом сняты.

    ⚠⚠ Живых баз репетиция не видит вовсе: php-контейнер получает `DATABASE_URL`
    только на репетиционную mysql в приватной сети. Сеть своя, имена со случайным
    суффиксом, уборка в `finally` — но **запускать стоит не тогда, когда живые
    миграции кто-то накатывает**: docker-сеть одна на хост, и имена контейнеров —
    общая namespace-квота.
    """
    wt = Path(worktree or repo).resolve()
    files = _newest_migrations(wt, int(prev))
    tables = mysql_rehearse_doctrine_tables(files)
    token = secrets.token_hex(3)
    net, ctr = f'mysql-rehearse-{token}', f'mysql-rehearse-{token}-db'
    log = Path(tempfile.gettempdir()) / f'mysql_rehearse_doctrine_{token}.log'
    secret = Path(tempfile.mkdtemp(prefix='mysql-rehearse-'))
    os.chmod(secret, 0o700)
    # Пароль живёт в файле, который монтируется в контейнер, — в argv его нет и в
    # журнал он не попадает: `ps` и дайджесты секретов не показывают.
    password = secrets.token_hex(12)
    (secret / 'pw').write_text(password + '\n', encoding='utf-8')
    (secret / 'my.cnf').write_text(f'[client]\nuser=root\npassword={password}\n', encoding='utf-8')
    os.chmod(secret / 'pw', 0o600)
    os.chmod(secret / 'my.cnf', 0o600)
    url = f'mysql://root:{password}@{ctr}:3306/{MYSQL_REHEARSE_DOCTRINE_DB}'
    source = (f'docker exec -i {ctr} mysql --defaults-extra-file=/mysql-pw/my.cnf '
              f'-N {MYSQL_REHEARSE_DOCTRINE_DB}')
    phases: list = []
    _log(log, f'репетиция Doctrine: {wt}; сети/контейнеры: {net} / {ctr}; '
              f'миграции под откат: {", ".join(one.name for one in files) or "—"}')
    try:
        _docker('network', 'create', net)
        _log(log, f'$ docker network create {net}')
        _docker('run', '-d', '--name', ctr, '--network', net, '--label', 'mysql-rehearse=1',
                '-v', f'{secret}:/mysql-pw:ro', '-e', 'MYSQL_ROOT_PASSWORD_FILE=/mysql-pw/pw',
                '-e', f'MYSQL_DATABASE={MYSQL_REHEARSE_DOCTRINE_DB}', image)
        _log(log, f'$ docker run -d --name {ctr} --network {net} … {image}')
        _wait_ready(source, log)
        up = ['bin/console', 'doctrine:migrations:migrate', '--no-interaction']
        phases.append(_phase(log, wt, repo, 'up', up, source, tables, sql_check, url, net, 1, timeout))
        phases.append(_phase(log, wt, repo, 'prev', up[:2] + ['prev'] + up[2:], source,
                             tables, sql_check, url, net, max(int(prev), 1), timeout))
        phases.append(_phase(log, wt, repo, 'up again', up, source, tables, sql_check, url, net, 1, timeout))
    finally:
        # Уборка обязана дойти до конца при любом исходе: висящая сеть и контейнер
        # мешают следующей репетиции и светятся живьём в `docker ps`.
        for cmd in (('rm', '-f', '-v', ctr), ('network', 'rm', net)):
            done = subprocess.run(['docker', *cmd], capture_output=True, text=True, timeout=60)
            _log(log, f'$ docker {" ".join(cmd)} → {done.returncode}')
        subprocess.run(['rm', '-rf', str(secret)])
    return _tally(files, tables, phases, sql_check, log)


def mysql_rehearse_doctrine_tables(paths: list) -> list:
    """Имена таблиц, которых касаются эти файлы миграций (up и down вместе)."""
    names = set()
    for one in paths:
        for chunk in _sql_chunks(Path(one)):
            names |= _statement_tables(chunk)
    return sorted(one for one in names if _NAME_OK.fullmatch(one) and one.upper() not in _JUNK)


def mysql_rehearse_doctrine_format(result: dict) -> str:
    """Отчёт репетиции словами: фазы с числами, хвосты упавших прогонов, вердикт и журнал."""
    lines = [result['digest']]
    for phase in result['phases']:
        if not phase['ok'] and phase['tail']:
            lines.append(f"── вывод фазы {phase['label']} (хвост)\n{phase['tail'][-1500:]}")
    lines.append(f'журнал: {result["log"]}')
    return '\n'.join(lines)


# ── детали реализации ──

def _phase(log: Path, wt: Path, repo: str, label: str, argv: list, source: str,
           tables: list, sql_check: str, url: str, net: str, runs: int, timeout: float) -> dict:
    """Одна фаза: прогоны console над копией, затем замер; упал прогон — дальше не идём, но меряем."""
    codes, tail = [], ''
    for step in range(max(runs, 1)):
        php = project_worktree_php_run(repo, str(wt), argv, env={'DATABASE_URL': url},
                                       networks=[net], timeout=timeout)
        codes.append(php['code'])
        tail = php['out']
        _log(log, f'── фаза {label}, прогон {step + 1}: {argv} → код {php["code"]}')
        _log(log, php['out'])
        if php['code'] != 0:
            break
    counts, check = _measure(source, tables, sql_check)
    _log(log, f'── замер после {label}: ' +
              ', '.join(f'{one}={counts[one]}' for one in sorted(counts)) +
              (f' | check={check}' if check is not None else ''))
    return {'label': label, 'codes': codes, 'ok': all(one == 0 for one in codes),
            'counts': counts, 'check': check, 'delta': {}, 'tail': tail}


def _measure(source: str, tables: list, sql_check: str) -> tuple:
    """Число строк каждой разбираемой таблицы и ответ контрольного запроса; `None` — «не знаю»."""
    counts = {}
    for one in tables:
        got = mysql_source_ask(source, f'SELECT COUNT(*) FROM `{one}`')
        counts[one] = int(got['out'].strip()) if got['ok'] and got['out'].strip().isdigit() else None
    check = None
    if sql_check:
        got = mysql_source_ask(source, sql_check)
        check = got['out'].strip() if got['ok'] else f'ошибка: {got["said"][:160]}'
    return counts, check


def _wait_ready(source: str, log: Path, timeout: float = MYSQL_REHEARSE_DOCTRINE_READY_TIMEOUT) -> None:
    """Ждать настоящую готовность базы: вопрос про сам каталог, а не `mysqladmin ping`.

    ⚠⚠ На этапе инициализации образ поднимает **временный** сервер без грантов —
    `mysqladmin ping` ему тоже отвечает «жив», и по пингу репетиция успевала
    стартовать до того, как создана база. Вопрос `SHOW DATABASES` к временному
    серверу даёт пустоту: ждём, пока в ответе появится наша база.
    """
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        got = mysql_source_ask(source, f'SHOW DATABASES LIKE \'{MYSQL_REHEARSE_DOCTRINE_DB}\'')
        if MYSQL_REHEARSE_DOCTRINE_DB in (got['out'] or ''):
            _log(log, f'база {MYSQL_REHEARSE_DOCTRINE_DB} готова')
            return
        time.sleep(2)
    raise RuntimeError(f'mysql-контейнер не стал готов за {timeout:g} с — журнал: {log}')


def _tally(files: list, tables: list, phases: list, sql_check: str, log: Path) -> dict:
    """Дельты между фазами, вердикт идемпотентности и дайджест."""
    before = {one: 0 for one in tables}
    for phase in phases:
        phase['delta'] = {one: (phase['counts'][one] - before[one]
                               if None not in (phase['counts'][one], before[one]) else None)
                          for one in phase['counts']}
        before = phase['counts']
    ups = [one for one in phases if one['label'].startswith('up')]
    idempotent = (all(one['ok'] for one in phases) and len(ups) == 2
                  and ups[0]['counts'] == ups[1]['counts'] and ups[0]['check'] == ups[1]['check'])
    lines = [f'миграции под откат: {", ".join(one.name for one in files)}',
             f'таблицы: {", ".join(tables) or "—"}']
    for phase in phases:
        cells = []
        for one in sorted(phase['counts']):
            delta = phase['delta'].get(one)
            mark = f' ({delta:+d})' if isinstance(delta, int) else ''
            cells.append(f'{one}={phase["counts"][one]}{mark}')
        lines.append(f"{phase['label']}: {' '.join(cells) or '—'}"
                     + (f' | check={phase["check"]}' if phase['check'] is not None else '')
                     + ('' if phase['ok'] else ' ⚠ прогоны упали'))
    if sql_check:
        lines.append(f'sql-check: {sql_check}')
    lines.append(f'идемпотентно up→down→up: {"да" if idempotent else "⚠ нет"}')
    return {'ok': all(one['ok'] for one in phases), 'idempotent': idempotent,
            'digest': '\n'.join(lines), 'log': str(log), 'tables': tables,
            'migrations': [str(one) for one in files], 'phases': phases}


def _newest_migrations(wt: Path, prev: int) -> list:
    """Последние `prev` файлов миграций (по имени — `Version…` сортируются по времени)."""
    where = _migrations_dir(wt)
    files = sorted(list(where.glob('*.php')) + list(where.glob('*.sql')))
    if not files:
        raise RuntimeError(f'в {where} нет файлов миграций')
    return files[-max(int(prev), 1):]


def _migrations_dir(wt: Path) -> Path:
    """Каталог миграций: из `doctrine_migrations.yaml`, иначе — где есть."""
    for cfg in (wt / 'config' / 'packages' / 'doctrine_migrations.yaml',
                wt / 'app' / 'config' / 'doctrine_migrations.yaml'):
        if cfg.is_file():
            found = re.search(r'%kernel\.project_dir%[/\\]([^\'"\s]+)',
                              cfg.read_text(encoding='utf-8', errors='replace'))
            if found and (wt / found.group(1)).is_dir():
                return wt / found.group(1)
    for one in ('migrations', 'src/Migrations', 'app/DoctrineMigrations'):
        if (wt / one).is_dir():
            return wt / one
    raise RuntimeError(f'каталог миграций в {wt} не найден (ни doctrine_migrations.yaml, ни migrations/)')


def _sql_chunks(path: Path) -> list:
    """Куски SQL миграции: весь файл для `.sql`, строковые литералы для PHP-класса."""
    text = path.read_text(encoding='utf-8', errors='replace')
    if path.suffix.lower() == '.sql':
        return [text]
    return [one for pair in re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", text) for one in pair if one]


def _statement_tables(chunk: str) -> set:
    """Таблицы одного SQL-выражения; комментарии выключены, ложные UPDATE-атрибуты отброшены."""
    clean = re.sub(r'--[^\n]*|/\*.*?\*/', ' ', chunk, flags=re.S)
    got = set()
    for found in _TABLE_IN.finditer(clean):
        keyword = found.group(0).upper()
        prefix = clean[max(0, found.start() - 24):found.start()]
        if keyword.startswith('UPDATE') and _NOT_UPDATE.search(prefix):
            continue
        got.add(found.group(1))
    if re.search(r'\bRENAME\s+TABLE\b', clean, re.I):     # `RENAME TABLE a TO b` трогает и b
        got |= {found.group(1) for found in
                re.finditer(r'\bTO\s+`?([A-Za-z0-9_]+)`?', clean, re.I)}
    return got


def _docker(*argv: str, timeout: float = 120) -> str:
    """Команда docker; ненулевой код — исключение с текстом ошибки (без argv целиком: там монтирования)."""
    done = subprocess.run(['docker', *argv], capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise RuntimeError(f'docker {" ".join(argv[:2])}: {(done.stderr or done.stdout).strip()[:300]}')
    return done.stdout


def _log(log: Path, text: str) -> None:
    """Строка в полный журнал репетиции (секретов здесь нет по построению — см. `_phase`)."""
    with log.open('a', encoding='utf-8') as handle:
        handle.write(str(text).rstrip() + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Репетиция Doctrine-миграций: одноразовая MySQL, up → prev → up, счётчики строк.')
    parser.add_argument('repo', help='основная выкладка PHP-проекта')
    parser.add_argument('--worktree', default='', help='копия, чей код крутится; пусто — сам repo')
    parser.add_argument('--image', default=MYSQL_REHEARSE_DOCTRINE_IMAGE, help='образ mysql')
    parser.add_argument('--prev', type=int, default=1, help='сколько миграций откатить')
    parser.add_argument('--sql-check', default='', help='контрольный запрос, сверяемый между фазами')
    parser.add_argument('--timeout', type=float, default=MYSQL_REHEARSE_DOCTRINE_PHASE_TIMEOUT,
                        help='секунд на фазу console-прогона')
    args = parser.parse_args()
    try:
        outcome = mysql_rehearse_doctrine_run(args.repo, args.worktree, args.image,
                                              args.prev, args.sql_check, args.timeout)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')
    print(mysql_rehearse_doctrine_format(outcome))
    raise SystemExit(0 if outcome['ok'] and outcome['idempotent'] else 1)

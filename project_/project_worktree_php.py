"""
PHP-сюита (phpunit / behat / phpcs / psalm) на коде worktree: контейнер php-fpm с копией вместо основной выкладки.

Карточка правки живёт в `git worktree`, а тесты PHP-проектов привыкли идти
`docker-compose exec php-fpm …` — тот монтирует **основную** выкладку, и правка
копии в прогон не попадает вообще: контейнер честно гоняет чужой код и честно
краснеет. Проги руками (`docker run -v …`) каждый раз собираются заново и теряют
то сети compose (база и очереди тогда недосягаемы), то `vendor/` (в копии его
`git worktree add` не заводит), то уборку — и `git worktree remove` встаёт на
`Permission denied` на корневом `var/`.

Здесь круг целиком: найти в compose основной выкладки сервис, который крутит PHP
(по слову `php` в образе или имени, с монтированием кода), собрать `docker run
--rm` с копией в корень кода из compose (`/usr/src/app` и т. п.), с `vendor/` и
прочим gitignored из основной выкладки на read-only, со всеми сетями и
переменными того сервиса — и прогнать сюиту. После — одноразовый контейнер той
же картинки сметает root-овые артефакты прогона из копии, иначе её не снять.

```bash
python -m project_.project_worktree test <репо> --worktree <копия> --suite phpunit --filter Rapyd
python -m project_.project_worktree test <репо> --worktree <копия> --suite behat --path behat/features/x
```
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commit_.commit_diff_master import commit_diff_master
from project_.project_ import project_git, project_main_root

# Сюиты и их бинари относительно корня кода (берётся первый существующий).
PROJECT_WORKTREE_PHP_SUITES = ('phpunit', 'behat', 'phpcs', 'psalm')
PROJECT_WORKTREE_PHP_BINS = {
    'phpunit': ('bin/phpunit', 'vendor/bin/phpunit'),
    'behat': ('vendor/bin/behat',),
    'phpcs': ('vendor/bin/phpcs',),
    'psalm': ('vendor/bin/psalm',),
}

# Что берётся из основной выкладки, когда в копии этого нет (gitignored):
# каталоги (vendor, кэш моста phpunit) монтируются на read-only, а файлы-конфиги
# (сюиты, `.env`) — через staging и cp внутри контейнера: docker для bind-файла
# создаёт точку монтирования на хосте, и внутри rw-монтажа копии оставил бы
# пустой behat.yml, который в следующий раз выглядел бы «файл есть».
PROJECT_WORKTREE_PHP_TAKE = ('vendor', 'bin/.phpunit', '.env', 'phpunit.xml', 'behat.yml',
                             'ruleset.xml', 'psalm.xml')

# Артефакты контейнера в копии: root-овые, из-за них `git worktree remove`
# падает на «Permission denied».
PROJECT_WORKTREE_PHP_ARTIFACTS = ('var', '.phpcs-cache', '.phpunit.cache')


def project_worktree_php_test(repo: str = '', worktree: str = '', suite: str = 'phpunit',
                              filt: str = '', path: str = '', timeout: float = 3600) -> dict:
    """Прогнать PHP-сюиту на коде worktree: php-fpm-контейнер над копией, дайджест числами и путь к полному журналу.

    Args:
        repo: основная выкладка PHP-проекта; пусто — основная для `worktree`.
        worktree: каталог копии; пусто — текущий.
        suite: `phpunit` (`bin/phpunit [--filter] [path]`), `behat`
               (`vendor/bin/behat [--name] [path]`), `phpcs` по изменённым
               относительно merge-base файлам (`--standard=ruleset.xml`, с
               дельтой против базы) или `psalm` по тем же изменённым файлам.
        filt: `--filter` у phpunit, `--name` у behat.
        path: путь фич/сюиты для behat и phpunit (`behat/features/x`).
        timeout: секунд на прогон; исчерпан — код -1 и журнал до обрыва.

    Returns:
        `{'suite', 'code', 'ok', 'digest', 'log', 'passed', 'failed', 'failures'}` —
        код возврата первого (главного) прогона, дайджест, файл полного журнала,
        счётчики и заголовки упавших тестов/сценариев.

    ⚠⚠ Контейнер пишет в копию от root (`var/`, кэши) — после прогона их
    вычищает одноразовый контейнер той же картинки; не вычистил — `git worktree
    remove` упадёт, и удалять придётся от sudo.
    ⚠ `vendor/` и конфиги на read-only из основной выкладки: `composer install`
    или правку этих файлов копия не переживёт — сначала в основной.
    ⚠ Правки в смонтированных на read-only конфигах (`behat.yml`) из копии не
    видно; файл из копии побеждает, как только он в копии есть.
    ⚠ База общая со стеком основной выкладки: сюита пишет в те же MySQL/Redis,
    что и основной стенд проекта.
    """
    wt = Path(worktree or Path.cwd()).resolve()
    root = Path(repo).resolve() if repo else project_main_root(root=str(wt))
    service, spec, config, error = _php_service(root)
    if error:
        raise ValueError(error)
    code_root, networks, image = _php_runtime(root, service, spec, config)
    mounts, staged = _php_mounts(wt, root, code_root)
    token = secrets.token_hex(3)
    log = Path(tempfile.gettempdir()) / f'project_worktree_php_{suite}_{token}.log'
    runs, note, base = _php_runs(suite, wt, root, code_root, filt, path)
    if runs is None:
        log.write_text(note + '\n', encoding='utf-8')
        return {'suite': suite, 'code': 0, 'ok': True, 'digest': note, 'log': str(log),
                'passed': 0, 'failed': 0, 'failures': []}
    with _php_stage(root, code_root, staged) as (stage_mount, pre):
        if base:
            with _php_base(root, base) as extra:
                runs = [(argv, extra if mark else '') for argv, mark in runs]
                output, rc = _docker_run(f'wt-php-{token}', image, networks, spec,
                                         mounts + stage_mount, code_root, pre, runs, timeout, log)
        else:
            output, rc = _docker_run(f'wt-php-{token}', image, networks, spec, mounts + stage_mount,
                                     code_root, pre, [(argv, '') for argv, _ in runs], timeout, log)
    _php_sweep(wt, image)
    info = _php_digest(suite, output, rc, filt)
    info.update(suite=suite, log=str(log))
    return info


def project_worktree_php_run(repo: str = '', worktree: str = '', argv: list = None,
                             env: dict = None, networks: list = None,
                             timeout: float = 1800) -> dict:
    """Произвольную команду того же php-контейнера — над кодом worktree; код, хвост вывода и полный журнал.

    Args:
        repo: основная выкладка PHP-проекта; пусто — основная для `worktree`.
        worktree: каталог копии; пусто — текущий.
        argv: команда внутри контейнера (рабочий каталог — корень кода); префикс
            `php -d memory_limit=-1` добавляется, если команда начинается с `php`.
        env: добавочные переменные прогона (например `DATABASE_URL`); пишутся в файл
            600 **после** переменных сервиса — перекрывают их.
        networks: добавочные docker-сети (например приватная с одноразовой базой).
        timeout: секунд на прогон; исчерпан — код -1 и журнал до обрыва.

    Returns:
        `{'ok', 'code', 'out', 'log'}` — код возврата, хвост вывода, путь к полному журналу.

    ⚠ Та же цена, что у сюиты: `vendor/` и конфиги — на read-only из основной
    выкладки, а root-овые артефакты прогона после сметаются из копии.
    """
    wt = Path(worktree or Path.cwd()).resolve()
    root = Path(repo).resolve() if repo else project_main_root(root=str(wt))
    service, spec, config, error = _php_service(root)
    if error:
        raise ValueError(error)
    code_root, nets, image = _php_runtime(root, service, spec, config)
    mounts, staged = _php_mounts(wt, root, code_root)
    token = secrets.token_hex(3)
    log = Path(tempfile.gettempdir()) / f'project_worktree_php_run_{token}.log'
    head = [str(one) for one in (argv or [])]
    if head and head[0] == 'php':
        head = head[:1] + ['-d', 'memory_limit=-1'] + head[1:]
    with _php_stage(root, code_root, staged) as (stage_mount, pre):
        output, rc = _docker_run(f'wt-php-{token}', image, nets + list(networks or []), spec,
                                 mounts + stage_mount, code_root, pre, [(head, '')], timeout,
                                 log, env)
    _php_sweep(wt, image)
    return {'ok': rc == 0, 'code': rc, 'out': output, 'log': str(log)}


# ── детали реализации ──

def _php_service(root: Path) -> tuple:
    """Сервис compose, который крутит PHP: по слову `php` в образе или имени, предпочитая тот, что монтирует код."""
    done = subprocess.run(['docker', 'compose', '--profile', '*', 'config', '--format', 'json'],
                          cwd=str(root), capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        return '', {}, {}, f'docker compose config в {root} не удался: {(done.stderr or done.stdout)[-300:]}'
    config = json.loads(done.stdout or '{}') or {}
    services = config.get('services') or {}

    def php_like(name: str, spec: dict) -> bool:
        text = f"{name} {(spec or {}).get('image', '')}".lower()
        return 'php' in text and not re.search(r'myadmin|adminer', text)

    named = {k: v or {} for k, v in services.items() if php_like(k, v)}
    mounted = sorted(k for k, v in named.items()
                     if any(_bind_root(one, root) for one in (v.get('volumes') or [])))
    pick = (mounted or sorted(named) or [''])[0]
    if not pick:
        return '', {}, config, (f'сервиса с php в compose-файлах {root} не сыскано '
                                f'(сервисы: {", ".join(sorted(services))})')
    return pick, named[pick], config, ''


def _php_runtime(root: Path, service: str, spec: dict, config: dict) -> tuple:
    """Корень кода в контейнере, имена сетей и образ этого php-сервиса."""
    code = next((v['target'] for v in spec.get('volumes') or [] if _bind_root(v, root)), '')
    if not code:                                   # один bind без явного корня — берём его
        binds = [v.get('target') for v in spec.get('volumes') or [] if v.get('type') != 'volume']
        code = binds[0] if len(binds) == 1 else ''
    if not code:
        raise ValueError(f'у сервиса {service!r} не найден mount корня {root} — куда ложится'
                         ' проект в контейнере, неизвестно')
    networks = [(((config.get('networks') or {}).get(key) or {}).get('name')) or key
                for key in (spec.get('networks') or {})]
    image = spec.get('image') or f"{(config.get('name') or root.name).lower()}-{service}"
    return code, networks, image


def _php_mounts(wt: Path, root: Path, code: str) -> tuple:
    """Монтирования прогона: копия в корень кода; чего в копии нет — каталоги ro из основной, файлы в staging.

    Bind-файл внутрь rw-монтажа копии ставить нельзя: под nonexistent-точку docker
    создаёт файл на хосте, и в копии остаётся 0-байтный behat.yml — в следующий
    раз он выглядел бы как «файл есть». С пустотой не спорим: пустой файл или
    пустой каталог считаем отсутствующим.
    """
    mounts, staged = ['-v', f'{wt}:{code}'], []
    for one in PROJECT_WORKTREE_PHP_TAKE:
        here, there = wt / one, root / one
        if not there.exists():
            continue
        if here.is_dir() and any(here.iterdir()):
            continue                                       # каталог есть в копии — берём его
        if here.is_file() and here.stat().st_size:
            continue                                       # непустой файл есть в копии — берёт приоритет
        if there.is_dir():
            mounts += ['-v', f'{there}:{code}/{one}:ro']    # точка под каталог хосту безвредна
        else:
            staged.append(one)
    return mounts, staged


@contextmanager
def _php_stage(root: Path, code: str, names: list):
    """Конфиги, которых в копии нет, — staging'ом в контейнер: монтирование и sh-префикс к каждому прогону.

    Именно cp, а не bind-файл: см. `_php_mounts` про 0-байтные точки монтирования.
    """
    if not names:
        yield [], ''
        return
    tmp = Path(tempfile.mkdtemp(prefix='php-stage-'))
    try:
        for one in names:
            shutil.copy2(str(root / one), str(tmp / one))
        pre = '; '.join(f'cp -f /php-stage/{one} {code}/{one}' for one in names)
        yield ['-v', f'{tmp}:/php-stage:ro'], pre
    finally:
        subprocess.run(['rm', '-rf', str(tmp)])


def _php_runs(suite: str, wt: Path, root: Path, code: str, filt: str, path: str) -> tuple:
    """Прогоны контейнером `[(argv, монтировать ли базу)]` и версии файлов для базового прогона.

    Первый прогон — главный, его код возврата и дайджест; второй (phpcs по
    merge-base) нужен только чтобы сказать «дельта», и его провал не виноват.
    """
    head = ['php', '-d', 'memory_limit=-1', _php_bin(suite, wt, root)]
    if suite == 'phpunit':
        return [(head + (['--filter', filt] if filt else []) + ([path] if path else []), False)], '', []
    if suite == 'behat':
        return [(head + (['--name', filt] if filt else []) + ([path] if path else []), False)], '', []
    files, base_files = _php_changed(wt)
    if not files:
        return None, 'нет изменённых относительно merge-base .php файлов — гнать нечего', []
    if suite == 'phpcs':
        runs = [(head + ['--standard=ruleset.xml', '--report=csv', *files], False)]
        if base_files:
            runs.append((['php', 'vendor/bin/phpcs', f'--standard={code}/ruleset.xml', '--report=csv',
                          *[f'/php-base/{one}' for one in base_files]], True))
        return runs, '', base_files
    return [(head + ['--no-progress', *files], False)], '', []


def _php_bin(suite: str, wt: Path, root: Path) -> str:
    """Бинарь сюиты относительно корня кода: что есть в копии, иначе — что приедет из основной."""
    for one in PROJECT_WORKTREE_PHP_BINS[suite]:
        if (wt / one).exists() or (root / one).exists():
            return one
    raise ValueError(f'бинарь сюиты {suite!r} не найден ни в копии {wt}, ни в основной {root}'
                     ' — выполни composer install в основной выкладке')


def _php_changed(wt: Path) -> tuple:
    """Изменённые относительно merge-base `.php` копии (с незакоммиченным и новым) и их пути в базе."""
    try:
        merge_base = commit_diff_master(root=str(wt))['merge_base']
    except RuntimeError as err:
        raise ValueError(f'база ветки не сыскана ({err}); phpcs/psalm считают дельту от merge-base') from err
    changed, base = [], []
    for line in project_git(wt, 'diff', '--name-status', merge_base).splitlines():
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        mark, new = parts[0][:1], parts[-1]
        old = parts[1] if mark in 'RC' else new        # переименованный в базе лежит под старым именем
        if new.endswith('.php') and mark != 'D' and (wt / new).is_file():
            changed.append(new)
            if mark != 'A' and old.endswith('.php'):
                base.append(old)
    changed += [one for one in project_git(wt, 'ls-files', '--others', '--exclude-standard').splitlines()
                if one.endswith('.php') and (wt / one).is_file()]
    return sorted(set(changed)), sorted(set(base))


def _php_sweep(wt: Path, image: str) -> None:
    """Смести root-овые артефакты контейнера из копии: иначе `git worktree remove` тонет в Permission denied."""
    rm = '; '.join(f'find "/sweep/{one}" -maxdepth 0 -user 0 -exec rm -rf {{}} +'
                   for one in PROJECT_WORKTREE_PHP_ARTIFACTS)
    chown = (f'find /sweep -xdev ! -user {os.getuid()} -print0 '
             f'| xargs -0 -r chown -h {os.getuid()}:{os.getgid()}')
    subprocess.run(['docker', 'run', '--rm', '-v', f'{wt}:/sweep', image, 'sh', '-c',
                    f'{rm}; {chown}'], capture_output=True, text=True, timeout=300)


def _php_digest(suite: str, output: str, code: int, filt: str = '') -> dict:
    """Дайджест прогона числами и заголовками упавшего; разбор — по формату отчёта каждой сюиты."""
    passed, failed, failures, digest = 0, 0, [], ''
    if suite == 'phpunit':
        ok = re.search(r'OK \((\d+) tests?', output)
        counts = dict(re.findall(r'(Tests|Assertions|Failures|Errors|Warnings|Skipped): (\d+)', output))
        if ok:
            passed = int(ok.group(1))
            digest = f'phpunit{" " + filt if filt else ""}: OK, {passed} тестов'
        elif counts:
            failed = int(counts.get('Failures', 0)) + int(counts.get('Errors', 0))
            passed = max(int(counts.get('Tests', 0)) - failed - int(counts.get('Skipped', 0)), 0)
            digest = f'phpunit{" " + filt if filt else ""}: ' + ', '.join(
                f'{k}: {counts[k]}' for k in ('Tests', 'Failures', 'Errors') if k in counts)
        else:
            digest = 'phpunit: итог не разобран — смотри журнал'
        failures = re.findall(r'^\d+\) (\S+)', output, re.M)[:20]
    elif suite == 'behat':
        sc = re.search(r'(\d+) scenarios? \(([^)]*)\)', output)
        counts = {word: int(num) for num, word in
                  re.findall(r'(\d+) (passed|failed|skipped|undefined|pending)', sc.group(2))} if sc else {}
        passed, failed = counts.get('passed', 0), counts.get('failed', 0)
        failures = re.findall(r'^[ \t]+(\S+\.feature:\d+)', output, re.M)[:20]
        digest = (f'behat: {sc.group(1)} сценариев ({sc.group(2)})' if sc
                  else 'behat: итог не разобран — смотри журнал')
    elif suite == 'phpcs':
        before, marker, after = output.partition('=== base run ===')
        rows_now, errors_now = _phpcs_count(before)
        failed = errors_now
        has_base = marker and bool(after.strip())
        rows_base, _ = _phpcs_count(after) if has_base else (0, 0)
        failures = [f'{one}: {msg}' for one, _, _, msg in
                    re.findall(r'"([^"]+)","(\d+)","[^"]*","(ERROR|WARNING)","([^"]*)"', before)[:20]]
        delta = f', дельта против базы {rows_now - rows_base:+d} (было {rows_base})' if has_base else ''
        digest = f'phpcs: находок {rows_now} (ошибок {errors_now}){delta}'
    else:
        bad = re.search(r'([\d,]+) errors? found', output)
        failures = re.findall(r'^(\S+\.php:\d+):\d+ (error \w+)', output, re.M)[:20]
        failed = int(bad.group(1).replace(',', '')) if bad else 0
        passed = 1 if not bad and 'No errors detected' in output else 0
        digest = (f'psalm: ошибок {failed}' if bad else
                  'psalm: чисто' if passed else 'psalm: прогон сломался — смотри журнал')
    broken = 'не разобран' in digest or 'сломался' in digest
    return {'ok': code == 0 and failed == 0 and not broken, 'code': code, 'passed': passed,
            'failed': failed, 'failures': failures, 'digest': digest}


def _phpcs_count(chunk: str) -> tuple:
    """(число записей, число ошибок) из csv-отчёта phpcs; пустой вывод — ноль находок."""
    rows = re.findall(r'"[^"]*","(\d+)","[^"]*","(ERROR|WARNING)"', chunk)
    return len(rows), sum(1 for _, kind in rows if kind == 'ERROR')


def _docker_run(name: str, image: str, networks: list, spec: dict, mounts: list, code: str,
                pre: str, runs: list, timeout: float, log: Path, extra_env: dict = None) -> tuple:
    """Каждый прогон — `docker run --rm`; stdout+stderr в журнал, вызывающему — хвост, код — от главного (первого) прогона.

    Секреты сервиса едут файлом 600, не аргументами: командная строка видна в `ps`.
    `extra_env` пишется в тот же файл **после** переменных сервиса — в `--env-file`
    последняя строка побеждает, так переопределяется `DATABASE_URL` сервиса.
    """
    cmd = ['docker', 'run', '--rm', '--name', name, '-w', code, *mounts]
    for net in networks:
        cmd += ['--network', net]
    for host in spec.get('extra_hosts') or []:
        cmd += ['--add-host', host.replace('=', ':', 1)]
    if spec.get('user'):
        cmd += ['--user', str(spec['user'])]
    chunks, rc = [], 0
    with tempfile.NamedTemporaryFile('w', suffix='.env', delete=False) as handle:
        os.chmod(handle.name, 0o600)
        for key, value in (spec.get('environment') or {}).items():
            if value is not None and '\n' not in str(value):
                handle.write(f'{key}={value}\n')
        for key, value in (extra_env or {}).items():
            if value is not None and '\n' not in str(value):
                handle.write(f'{key}={value}\n')
        handle.flush()                            # иначе docker увидит пустой env-файл
        try:
            with log.open('w', encoding='utf-8') as journal:
                try:
                    for index, (argv, extra) in enumerate(runs, start=1):
                        if pre:                   # сначала разложить staging-конфиги, потом сюита
                            argv = ['sh', '-c', f'{pre}; exec "$@"', 'wt-php', *argv]
                        journal.write(f'\n=== {"base run" if extra else "run"} {index} ===\n')
                        done = subprocess.run(cmd + list(extra) + ['--env-file', handle.name, image, *argv],
                                              capture_output=True, text=True, timeout=timeout)
                        text = (done.stdout or '') + (done.stderr or '')
                        journal.write(f'$ docker run … {image} {" ".join(argv)}\n{text}\n')
                        chunks.append(('=== base run ===\n' if extra else '') + text)
                        if index == 1:
                            rc = done.returncode
                except subprocess.TimeoutExpired as err:
                    partial = err.stdout.decode('utf-8', 'replace') if isinstance(err.stdout, bytes) else (err.stdout or '')
                    journal.write(f'{partial}\nпрогон снят по таймауту {timeout:g} с\n')
                    chunks.append(f'{partial}\nпрогон снят по таймауту {timeout:g} с')
                    rc = -1
        finally:
            os.unlink(handle.name)
            # ⚠ Убитый по таймауту клиент `docker run` оставляет контейнер живым.
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=60)
    return '\n'.join(chunks)[-6000:], rc


@contextmanager
def _php_base(root: Path, files: list):
    """Версии файлов из merge-base во временном каталоге: по ним phpcs меряет дельту, а не чужую правку.

    Отдаёт доп.монтирование на `/php-base`; сам каталог живёт только внутри блока.
    """
    merge_base = commit_diff_master(root=str(root))['merge_base']
    archive = subprocess.run(['git', 'archive', merge_base, '--', *files], cwd=str(root),
                             capture_output=True, timeout=120)
    if archive.returncode != 0:
        raise ValueError(f'git archive {merge_base} не удался: {archive.stderr[-300:].decode("utf-8", "replace")}')
    tmp = Path(tempfile.mkdtemp(prefix='php-base-'))
    try:
        tarfile.open(fileobj=BytesIO(archive.stdout)).extractall(path=str(tmp))  # архив свой, из git
        yield ['-v', f'{tmp}:/php-base']
    finally:
        subprocess.run(['rm', '-rf', str(tmp)])


def _bind_root(volume: dict, root: Path) -> bool:
    """Монтирование ли это корня репозитория (`.:/usr/src/app`, абсолютный путь — одно и то же)."""
    source = str(volume.get('source') or '')
    if not source:
        return False
    way = Path(source)
    return (way if way.is_absolute() else root / source).resolve() == root


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='PHP-сюита на worktree: php-fpm-контейнер над копией, дайджест и журнал.')
    ap.add_argument('repo', nargs='?', default='', help='основная выкладка; пусто — основная для --worktree')
    ap.add_argument('--worktree', default='', help='каталог копии; пусто — текущий')
    ap.add_argument('--suite', choices=list(PROJECT_WORKTREE_PHP_SUITES), default='phpunit',
                    help='что гнать')
    ap.add_argument('--filter', default='', help='phpunit --filter / behat --name')
    ap.add_argument('--path', default='', help='путь фич/сюиты (phpunit, behat)')
    ap.add_argument('--timeout', type=float, default=3600, help='секунд на прогон')
    ns = ap.parse_args()
    try:
        result = project_worktree_php_test(ns.repo, ns.worktree, ns.suite, ns.filter, ns.path, ns.timeout)
    except ValueError as err:
        raise SystemExit(f'ошибка: {err}')
    print(result['digest'])
    print(f'журнал: {result["log"]}')
    raise SystemExit(result['code'])

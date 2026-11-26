"""
Панель задачи над worktree: поднять, дождаться здоровья, снять — и ссылки целы.

Выкладка задачи временная: контейнер монтирует её каталог вместо репозитория,
и пока в каталоге нет ссылок на хостовые пути (`.env`, `.venv`, общий слой),
контейнер видит пустой `/app` и падает на `Could not import module`, что про
исчезнувший worktree не говорит ничего. Ссылки и переменную каталога подставляли
руками, сеть compose создавали новую — и ловили «all predefined address pools
have been fully subnetted» на машине, где мостовых сетей уже под тридцать.

Здесь круг целиком: проверить ссылки, поднять сервис от корня **основной**
выкладки (compose помнит проект от своего каталога — сеть берётся готовая),
передать переменной путь каталога задачи, дождаться `/health`, вернуть base url
для сюит. На выходе — снять сервис, пока каталог не удалён.

Второй путь — `project_worktree_test`: **одноразовый** сервер на свою копию и прогон
тестов против него. Сервис задачи один на машину (QA конвейера), а параллельных
копий бывает несколько (`dsh-board fanout --worktree`) — им нужен каждому свой.

Третий — PHP-проектам, где постоянного сервиса задачи нет: сюита (`phpunit`,
`behat`, `phpcs`, `psalm`) прогоняется php-fpm-контейнером над копией, а не
основной выкладкой, куда та по привычке `compose exec` смонтирована. Механика —
`project_worktree_php.py`.

```bash
python -m project_.project_worktree test tests/test_x.py --worktree <копия>
python -m project_.project_worktree test <репо> --worktree <копия> --suite phpunit --filter Rapyd
```
"""
import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile

from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_.http_pool import http_pool_client
from project_.project_ import project_git
from project_.project_worktree_php import project_worktree_php_test

# Что обязано быть ссылкой в каталоге задачи: compose монтирует каталог задачи
# целиком, а симлинки из основной выкладки в него не едут — без них `.env`
# пустой, интерпретатор не тот, общий слой не виден.
PROJECT_WORKTREE_LINKS = ('.env', '.env.local', '.venv', 'py_modules')

# Имя переменной, из которой compose берёт каталог задачи. Другой проект зовёт
# своей — поэтому именем можно поделиться, а не держать зашитым.
PROJECT_WORKTREE_DIR_VAR = 'PROJECT_TASK_WORKTREE_DIR'


async def project_worktree_up(worktree: str = '', service: str = 'uvicorn_task',
                              dir_var: str = PROJECT_WORKTREE_DIR_VAR,
                              links: tuple = PROJECT_WORKTREE_LINKS,
                              timeout: float = 120) -> str:
    """Поднять сервер задачи над каталогом задачи и вернуть его base url.

    Сервис ищется в compose-проекте **основной** выкладки (её корень — общий
    `.git` каталога задачи, не место модуля; оттого сеть берётся готовая, а не
    заводится новая), поднимается с каталогом задачи в `dir_var`, ждёт `/health`
    и отдаёт адрес со своего опубликованного порта — тем же путём сюиты берут
    `BASE_URL`.

    Args:
        worktree: каталог задачи; пусто — текущий (обычно его и снимают).
        service: подстрока имени compose-сервиса задачи.
        dir_var: имя переменной окружения, из которой compose берёт каталог.
        links: чего не хватает в каталоге задачи из корня основной выкладки.
        timeout: секунд на подъём и здоровье.

    Returns:
        base url поднятой панели задачи — для `BASE_URL` сюит.

    ⚠⚠ Каталог задачи временный: сервис переживает свой каталог и при следующем
    старте монтирует точку заново. Сняли каталог — снимайте сервис
    `project_worktree_down()`, а не бросайте контейнер над пустым `/app`.
    """
    wt = Path(worktree or Path.cwd()).resolve()
    root = _main_of(wt)
    _ensure_links(wt, root, links)

    env = {**os.environ, dir_var: str(wt)}
    row = _found(_ps(root), service)
    name = _name(row) if row else service
    cmd = ['restart', name] if row else ['up', '-d', service]
    subprocess.run(['docker', 'compose', *cmd], cwd=str(root), env=env,
                   check=True, capture_output=True, text=True, timeout=timeout)

    return await _alive(_base(_found(_ps(root), name)), timeout=timeout)


def project_worktree_down(service: str = 'uvicorn_task') -> str:
    """Снять сервис задачи: контейнер больше не висит над удалённым каталогом."""
    root = _main_of(Path.cwd())
    row = _found(_ps(root), service)
    name = _name(row) if row else service
    for cmd in (['stop', name], ['rm', '-f', name]):
        subprocess.run(['docker', 'compose', *cmd], cwd=str(root),
                       check=True, capture_output=True, text=True, timeout=60)

    return f'сервис {name} снят'


def project_worktree_test(targets: list, worktree: str = '', service: str = 'uvicorn_task',
                          pytest_args: list = None, timeout: float = 120,
                          test_timeout: float = 1800) -> dict:
    """Прогнать тесты на коде своей копии репо: свой одноразовый сервер, сюита против него, уборка.

    Сервер — контейнер того же образа, что `service` в compose основной выкладки, с той
    же сетью, переменными и командой, но с копией вместо `/app`, на свободном порту
    петли и со своим именем: параллельные копии не мешают ни друг другу, ни общему
    серверу задачи. Сюита идёт с хоста интерпретатором основной выкладки
    (`.venv`), `BASE_URL` — на этот сервер; контейнер снимается в любом исходе.

    Args:
        targets: что гонять — файлы/каталоги тестов относительно копии.
        worktree: каталог копии; пусто — текущий.
        service: compose-сервис, чьё описание берётся за образец.
        pytest_args: добавочные флаги pytest (`['-k', 'x']`).
        timeout: секунд на подъём сервера; test_timeout — на саму сюиту.

    Returns:
        `{code, output, base}` — код возврата pytest (0 — зелено), хвост вывода, адрес.

    ⚠ База общая с основным стендом: тесты, как и всегда, заводят и чистят свои
    данные сами. ⚠ Секреты `.env` идут в контейнер файлом 600, не аргументами
    `docker run`: командная строка видна в `ps`.
    """
    wt = Path(worktree or Path.cwd()).resolve()
    root = _main_of(wt)
    _ensure_links(wt, root, PROJECT_WORKTREE_LINKS)
    spec, network = _service_spec(root, service, wt)
    name = f'{service}-{secrets.token_hex(3)}'
    shared = _empty_submodules(wt, root)
    _run_container(name, spec, network, wt, shared)
    try:
        try:
            base = asyncio.run(_alive(_published(name), timeout=timeout))
        except (TimeoutError, ValueError) as error:
            logs = subprocess.run(['docker', 'logs', '--tail', '25', name], capture_output=True,
                                  text=True, timeout=20)
            raise TimeoutError(f'{error}\nжурнал контейнера:\n{(logs.stdout + logs.stderr)[-3000:]}') from error
        env = {**os.environ, 'BASE_URL': base}
        shim = tempfile.mkdtemp(prefix='worktree-shim-')
        env['PYTHONPATH'] = _host_pythonpath(spec, root, shared, shim, env.get('PYTHONPATH', ''))
        python = str(root / '.venv' / 'bin' / 'python3')
        done = subprocess.run([python, '-m', 'pytest', *targets, '-q', *(pytest_args or [])],
                              cwd=str(wt), env=env, capture_output=True, text=True, timeout=test_timeout)
        output = (done.stdout + done.stderr)[-6000:]
        return {'code': done.returncode, 'output': output, 'base': base}
    finally:
        _give_back(name, shared)
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=60)
        if 'shim' in locals():
            shutil.rmtree(shim, ignore_errors=True)


# ── детали реализации ──

def _service_spec(root: Path, service: str, worktree: Path) -> tuple:
    """Описание сервиса из `docker compose config` основной выкладки и имя его сети."""
    env = {**os.environ, PROJECT_WORKTREE_DIR_VAR: str(worktree)}
    done = subprocess.run(['docker', 'compose', '--profile', '*', 'config', '--format', 'json'],
                          cwd=str(root), env=env, capture_output=True, text=True, timeout=60)
    config = json.loads(done.stdout or '{}')
    spec = (config.get('services') or {}).get(service)
    if not spec:
        raise ValueError(f'сервиса {service!r} нет в compose основной выкладки {root}')
    if not spec.get('image') and spec.get('build'):
        # ⚠ Сервис, описанный только `build:`: `config --format json` не
        # подставляет **умолчное имя его образа**, а `docker run` его хочет —
        # KeyError на ровном месте. Compose даёт такому образу имя
        # `<проект>-<сервис>`, где проект — `name:` из файла или каталог;
        # берём то же правило. Образ обязан быть собран (`compose build`),
        # иначе `docker run` скажет это словами.
        spec['image'] = f"{config.get('name') or root.name.lower()}-{service}"
    nets = list((spec.get('networks') or {}).keys())
    network = ((config.get('networks') or {}).get(nets[0]) or {}).get('name') if nets else None
    return spec, network


def _empty_submodules(worktree: Path, root: Path) -> list:
    """Подмодули (`.gitmodules`), пустые в копии — `git worktree add` их не заводит, — но живые в основной.

    ⚠ Не ссылкой в копии: git увидит подмену каталога, копия станет «грязной» — её не
    снимет уборка и унесёт в коммит `land`. Подаются монтированием и `PYTHONPATH`.
    """
    names = project_git(root, 'config', '--file', '.gitmodules', '--get-regexp', r'\.path$') or ''
    paths = [line.split(maxsplit=1)[1] for line in names.splitlines() if ' ' in line]
    rows = []
    for one in dict.fromkeys([*paths, *PROJECT_WORKTREE_LINKS]):
        here, there = worktree / one, root / one
        if here.is_dir() and not here.is_symlink() and not any(here.iterdir()) and there.is_dir():
            rows.append(one)
    return rows


def _host_pythonpath(spec: dict, root: Path, shared: list, shim: str, inherited: str) -> str:
    """`PYTHONPATH` сюиты на хосте — та же раскладка, что у приложения в контейнере.

    Подмодули, пустые в копии, подкладываются ссылками во временную папку `shim`: пакет
    (`dashboard.src…`) находится через неё, а копия не трогается. Записи `PYTHONPATH`
    контейнера вида `/app/<подмодуль>` (`/app/py_modules`) — каталог основной выкладки.
    ⚠ Не корень подмодуля целиком: у `dashboard` внутри свой `src`, и он перекрыл бы
    `src` проекта — `No module named 'src.ai'`.
    """
    for one in shared:
        os.symlink(root / one, os.path.join(shim, one))
    rows = [shim]
    for entry in _container_pythonpath(spec).split(':'):
        name = entry[len('/app/'):] if entry.startswith('/app/') else ''
        if name in shared:
            rows.append(str(root / name))
    return os.pathsep.join([*rows, inherited]).rstrip(os.pathsep)


def _container_pythonpath(spec: dict) -> str:
    """`PYTHONPATH` приложения: из описания сервиса, а нет там — из самого образа."""
    value = (spec.get('environment') or {}).get('PYTHONPATH')
    if value:
        return str(value)
    done = subprocess.run(['docker', 'image', 'inspect', spec.get('image') or '', '--format',
                           '{{range .Config.Env}}{{println .}}{{end}}'],
                          capture_output=True, text=True, timeout=30)
    for line in (done.stdout or '').splitlines():
        if line.startswith('PYTHONPATH='):
            return line.split('=', 1)[1]
    return ''


def _give_back(name: str, shared: list) -> None:
    """Вернуть хозяину копии то, что контейнер (root) в ней создал: журналы `data/log` и пр.

    ⚠ Иначе `git worktree remove` падает на «Permission denied», и уборка копий встаёт.
    Подмодули основной выкладки, смонтированные в `/app`, не трогаются.
    """
    skip = ' '.join(f"-path /app/{one} -prune -o" for one in shared)
    script = f"find /app -xdev {skip} -user 0 -print0 | xargs -0 -r chown {os.getuid()}:{os.getgid()}"
    subprocess.run(['docker', 'exec', name, 'sh', '-c', script], capture_output=True, timeout=120)


def _run_container(name: str, spec: dict, network: str, worktree: Path, shared: list = ()) -> None:
    """`docker run -d` по описанию сервиса: копия в `/app`, порт петли — свободный."""
    cmd = ['docker', 'run', '-d', '--name', name, '-p', '127.0.0.1::8000']
    if network:
        cmd += ['--network', network]
    for volume in spec.get('volumes') or []:
        source = str(worktree) if volume.get('target') == '/app' else volume.get('source')
        cmd += ['-v', f"{source}:{volume['target']}"]
    root = _main_of(worktree)
    for one in shared:
        cmd += ['-v', f'{root / one}:/app/{one}']
    for host in spec.get('extra_hosts') or []:
        cmd += ['--add-host', host.replace('=', ':', 1)]
    with tempfile.NamedTemporaryFile('w', suffix='.env', delete=False) as handle:
        os.chmod(handle.name, 0o600)
        for key, value in (spec.get('environment') or {}).items():
            if value is not None and '\n' not in str(value):
                handle.write(f'{key}={value}\n')
    try:
        command = spec.get('command') or []
        subprocess.run(cmd + ['--env-file', handle.name, spec['image'], *command],
                       check=True, capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(handle.name)


def _published(name: str) -> str:
    """Адрес контейнера на петле по его опубликованному порту 8000."""
    done = subprocess.run(['docker', 'port', name, '8000/tcp'], capture_output=True, text=True, timeout=20)
    line = (done.stdout or '').splitlines()[0] if done.stdout else ''
    if not line:
        raise ValueError(f'контейнер {name} без опубликованного порта')
    return f'http://127.0.0.1:{line.rsplit(":", 1)[-1]}'


def _main_of(checkout: Path) -> Path:
    """Корень основной выкладки для этого checkout — родитель общего `.git`.

    ⚠ Не `project_main_root()`: тот берёт корень от места **модуля**, а здесь
    нужен корень **переданной выкладки** — git отвечает на `--git-common-dir` из
    самой копии, откуда бы ни был загружен этот модуль.
    """
    common = project_git(checkout, 'rev-parse', '--git-common-dir')
    if not common:
        return checkout
    path = Path(common)
    if not path.is_absolute():
        path = (checkout / path).resolve()
    return path.parent if path.name == '.git' else checkout


def _ensure_links(worktree: Path, root: Path, links: tuple) -> None:
    """Чего нет в каталоге задачи из основной выкладки — то её симлинк на хост."""
    for one in links:
        here, there = worktree / one, root / one
        if here.is_symlink() or here.exists() or not there.exists():
            continue
        here.symlink_to(there)


def _ps(root: Path) -> list:
    """Строки `docker compose ps --format json` из корня основной выкладки."""
    done = subprocess.run(['docker', 'compose', 'ps', '--format', 'json'],
                          cwd=str(root), capture_output=True, text=True,
                          timeout=20)
    rows = []
    for line in (done.stdout or '').splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _found(rows: list, service: str) -> dict:
    """Поднятая строка ps по подстроке имени сервиса; пусто — сервис не поднят."""
    for row in rows:
        if service in _name(row):
            return row
    return {}


def _name(row: dict) -> str:
    """Имя сервиса из строки ps: `Service`, а в старых форматах — из `Names`."""
    if row.get('Service'):
        return row['Service']
    return (row.get('Names') or [''])[0].lstrip('/')


def _base(row: dict) -> str:
    """http-адрес панели из её строки ps: первый опубликованный порт.

    ⚠ `Ports` в этом формате — строка `'0.0.0.0:8013->8000/tcp, ...'`, а не
    список: обходить её по буквам — не найти в букве `->` и потерять порт.
    """
    ports = row.get('Ports') or ''
    for part in (ports if isinstance(ports, str) else ','.join(ports)).split(','):
        if '->' in part:
            return f'http://127.0.0.1:{part.split("->")[0].rsplit(":", 1)[-1]}'
    raise ValueError(f'панель {_name(row)!r} без опубликованного порта в ps')


async def _alive(base: str, timeout: float) -> str:
    """Ждать ответа `/health`: любой ответ — признак живого приложения.

    ⚠ Панель задачи под сторожем и на `/health` отвечает **401**: ждать 2xx,
    как `uvicorn_dev_health`, — не дождаться никогда. Здесь же connection
    refused — настоящий мёртвый знак, а любой HTTP-ответ — живой.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last = ''
    while True:
        try:
            async with http_pool_client(timeout=3) as c:
                await c.get(f'{base}/health')
            return base                      # любой HTTP-ответ — панель жива
        except Exception as err:
            last = str(err) or type(err).__name__
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f'панель {base} не подала признаков жизни за '
                               f'{timeout:g} с: {last}')
        await asyncio.sleep(0.5)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Панель задачи над worktree: поднять и дождаться '
                    'здоровья, снять.')
    ap.add_argument('command', choices=['up', 'down', 'test'],
                    help='up — поднять над каталогом и вернуть base url; '
                         'down — снять сервис; test — свой сервер и тесты на нём')
    ap.add_argument('targets', nargs='*', help='test: файлы или каталоги тестов')
    ap.add_argument('-k', default='', help='test: фильтр pytest -k')
    ap.add_argument('--worktree', default='', help='каталог задачи; пусто — '
                    'текущий')
    ap.add_argument('--service', default='uvicorn_task',
                    help='подстрока имени compose-сервиса')
    ap.add_argument('--suite', choices=['phpunit', 'behat', 'phpcs', 'psalm'], default='',
                    help='test: PHP-сюита над копией вместо сервера с pytest; '
                         'позиционный аргумент — основная выкладка репо')
    ap.add_argument('--filter', default='', help='test --suite: phpunit --filter / behat --name')
    ap.add_argument('--path', default='', help='test --suite: путь фич/сюиты (behat, phpunit)')
    ap.add_argument('--timeout', type=float, default=None,
                    help='секунды (120 для сервера, 3600 для PHP-сюиты)')
    ns = ap.parse_args()
    try:
        if ns.command == 'test' and ns.suite:
            result = project_worktree_php_test(*(ns.targets or [''])[:1], ns.worktree, ns.suite,
                                               ns.filter, ns.path, ns.timeout or 3600)
            print(result['digest'])
            print(f'журнал: {result["log"]}')
            raise SystemExit(result['code'])
        if ns.command == 'test':
            result = project_worktree_test(ns.targets or ['tests'], ns.worktree, ns.service,
                                           ['-k', ns.k] if ns.k else None, timeout=ns.timeout or 120)
            print(result['output'])
            raise SystemExit(result['code'])
        if ns.command == 'up':
            print(asyncio.run(project_worktree_up(
                ns.worktree, ns.service, timeout=ns.timeout or 120)))
        else:
            print(project_worktree_down(ns.service))
    except (RuntimeError, TimeoutError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

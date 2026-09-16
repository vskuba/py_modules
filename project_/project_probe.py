"""
Доменный код наружу из контейнера: тем же окружением панели, но адресами с хоста.

Прогнать функцию домена разовым скриптом — то же окружение, что у панели:
`MYSQL_HOST`, `REDIS_URL`... Только в контейнере они указывают на имена сервисов
(`persona_mysql`), а снаружи эти имена не резолвятся, и внутренний порт не тот,
что опубликован. Собирать этот круг руками — ошибка за ошибкой: угаданное имя
переменной, внутренний адрес вместо внешнего, порт не из таблицы публикации.

Инструмент делает круг сам: читает окружение поднятого контейнера, заменяет в
нём имена сервисов на `127.0.0.1`, а внутренние порты — на опубликованные
(из `docker compose ps`), и исполняет кусок кода интерпретатором проекта.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_.project_ import project_main_root, project_python


def project_probe_env(service: str = 'uvicorn', root=None) -> dict:
    """Окружение контейнера панели, но адресами reachable с хоста.

    Имена compose-сервисов в значениях (хосты, URL) меняются на `127.0.0.1`,
    внутренние порты — на опубликованные тем же контейнером.

    Args:
        service: подстрока имени сервиса, чьё окружение берётся (панель — `uvicorn`).
        root: корень проекта; пусто — тот, где лежит этот модуль (в проекте —
            верный; в самостоятельной выкачке указывать явно).

    Returns:
        dict переменных окружения; ⚠ переменные, названные не в честь хостов,
        переносятся как есть — имена сервисов здесь меняются всегда, даже там,
        где это лишнее: код снаружи этого не заметит, а внутри не бывает.
    """
    base = Path(root) if root else project_main_root()
    try:
        ps = subprocess.run(['docker', 'compose', 'ps', '--format', 'json'],
                            cwd=str(base), capture_output=True,
                            text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        ps = None
    if not ps or ps.returncode != 0:
        raise RuntimeError('compose не ответил на ps — поднята ли панель?')
    services = {}
    for line in ps.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        first = (row.get('Names') or [''])[0]
        name = row.get('Service') or (first.get('Name', '') if isinstance(first, dict)
                                      else first)
        ports = {str(p['TargetPort']): str(p['PublishedPort']) for p in
                 row.get('Publishers') or []
                 if p.get('TargetPort') and p.get('PublishedPort')}
        if name:
            services[name] = ports
    running = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'],
                             capture_output=True, text=True, timeout=10)
    names = [n for n in running.stdout.split() if service in n
             and any(s in n for s in services)]
    if not names:
        raise RuntimeError(f'контейнер сервиса {service!r} не поднят — сначала up')
    raw = subprocess.run(['docker', 'inspect', '-f', '{{json .Config.Env}}',
                          names[0]], capture_output=True, text=True,
                         timeout=10).stdout
    env = dict(item.split('=', 1) for item in json.loads(raw) if '=' in item)
    for key, value in list(env.items()):
        fixed = value
        for name, ports in services.items():
            if name in fixed:
                fixed = fixed.replace(name, '127.0.0.1')
            for internal, published in ports.items():
                if fixed == internal:
                    fixed = published  # голый порт: MYSQL_PORT=3306 снаружи — 3321.
                else:
                    fixed = fixed.replace(f':{internal}', f':{published}')
        env[key] = fixed
    env.pop('PYTHONPATH', None)
    return {**os.environ, **env}


def project_probe(code: str, service: str = 'uvicorn', root=None,
                  is_async: bool = None) -> str:
    """Выполнить кусок доменного кода окружением панели (снаружи контейнера) и вернуть вывод.

    Тело пишется так, будто оно уже внутри корутины: `await` в коде — и обёртка
    `asyncio.run` добавляется сама.

    Args:
        code: текст программы (обычно вызов доменной функции + print).
        service: чьим окружением запускать (по умолчанию — панели).
        root: корень проекта; пусто — тот, где лежит этот модуль.
        is_async: None — определить по слову `await` в коде.

    Returns:
        stdout процесса.

    Raises:
        RuntimeError: сервис не поднят; код возврата процесса ≠ 0 — хвост
        stderr в тексте ошибки.
    """
    base = Path(root) if root else project_main_root()
    if is_async is None:
        is_async = 'await ' in code
    body = ('\n'.join('    ' + l for l in code.splitlines()) or '    pass')
    source = ('import asyncio\n\n\nasync def _main():\n' + body +
              '\n\n\nasyncio.run(_main())\n') if is_async else code
    python = base / '.venv' / 'bin' / 'python'
    python = python if python.is_file() else project_python()
    env = project_probe_env(service, root)
    env['PYTHONPATH'] = os.pathsep.join([str(base), str(base / 'py_modules')])
    done = subprocess.run([str(python), '-u', '-c', source], cwd=str(base),
                          env=env, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f'код вернулся с {done.returncode}: '
                           f'{done.stderr.strip()[-500:]}')
    return done.stdout


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Кусок доменного кода окружением поднятой панели, снаружи '
                    'контейнера.')
    ap.add_argument('-a', '--async-code', required=True, metavar='КОД',
                    help='код (тело корутины оборачивается само)')
    ap.add_argument('--service', default='uvicorn')
    ap.add_argument('--root', default='', help='корень проекта; пусто — от модуля')
    ns = ap.parse_args()
    try:
        print(project_probe(ns.async_code, ns.service, ns.root or None), end='')
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

"""Рестарт сервиса с подтверждением и журнал по шаблону: без сна и без grep руками.

«После правки кода — рестарт панели» — обязанность, а рестарт вслепую — половина
проверки. `docker compose restart` возвращается раньше, чем сервис поднимется:
без ожидания следующая команда (зонд страницы, сюита) упрётся в мёртвый порт и
начнёт врать про правку, а не про рестарт. Заодно журнал: «дошло ли до
контейнера» и «что он наговорил с последней правки» сейчас — это `docker
compose logs <svc> | grep` с угаданным `--since`.

`docker_echo` меряет доставленность числами, `compose_x` даёт контейнеру
команду; здесь — жизненный цикл: поднять-доказать и почитать, что он говорил.

⚠ Ожидание — по строке готовности или по статусу контейнера, но не по
`sleep`: сон не гарантирует ничего и всегда одновременно и короткий, и длинный.
"""
import argparse
import json
import re
import subprocess
import sys
import time


def docker_compose_restart_wait(service: str, ready: str = '', timeout: float = 90.0,
                                root: str = '') -> dict:
    """Рестарт сервиса и ожидание, пока он не скажет, что жив.

    Args:
        service: имя сервиса compose-стека.
        ready: регулярка строки готовности в журнале сервиса ('Uvicorn running',
            'Application startup complete'); пусто — верить статусу контейнера
            (running, и — если задан — health=healthy).
        timeout: потолок ожидания в секундах.
        root: корень проекта с `docker-compose.yml`; пусто — текущий каталог.

    Returns:
        {'ok': bool, 'state': str, 'log': [последние строки журнала после
        рестарта], 'error': str}. `ok=False` — не исключение: таймаут и
        упавший сервис это ответ, а не авария вызывающего.

    ⚠ Журнал для проверки `ready` берётся с момента рестарта (`--since` от
    него), иначе в него попадают «признаки готовности» прошлой жизни сервиса и
    ok=True возвращается до того, как сервис поднялся на этот раз.
    """
    t0 = time.time()
    r = subprocess.run(['docker', 'compose', 'restart', service],
                       capture_output=True, text=True, timeout=timeout,
                       cwd=root or None)
    if r.returncode != 0:
        return {'ok': False, 'state': 'restart-failed', 'log': [],
                'error': (r.stderr or r.stdout).strip() or
                         f'compose не поднял сервис «{service}»'}
    cid = _container_id(service, root)
    if not cid:
        return {'ok': False, 'state': 'no-container', 'log': [],
                'error': f'контейнер сервиса «{service}» не найден — поднят ли стек?'}
    pattern = re.compile(ready) if ready else None
    # ⚠ отсчёт — по monotonic: `t0` это epoch, сравнивать его с часами
    # аптайма значит ждать вечно
    deadline = time.monotonic() + timeout
    state, log = '', []
    while time.monotonic() < deadline:
        log, _ = _logs_since(cid, t0, tail=0)
        if pattern:
            if any(pattern.search(line) for line in log):
                return {'ok': True, 'state': 'ready', 'log': log[-15:], 'error': ''}
        status = subprocess.run(
            ['docker', 'inspect', '-f',
             '{{if .State.Health}}{{.State.Health.Status}}{{else}}'
             '{{.State.Status}}{{end}}', cid],
            capture_output=True, text=True, timeout=10).stdout.strip()
        state = status or state
        if not pattern and status in ('running', 'healthy'):
            return {'ok': True, 'state': status, 'log': log[-15:], 'error': ''}
        if status == 'exited':
            return {'ok': False, 'state': status, 'log': log[-15:],
                    'error': f'сервис «{service}» после рестарта упал'}
        time.sleep(1.0)
    return {'ok': False, 'state': state or 'waiting', 'log': log[-15:],
            'error': f'за {timeout:g} c не готов: сервис «{service}» '
                     f'не сказал {ready!r}' if pattern else
                     f'за {timeout:g} c сервис «{service}» не поднялся'}


def docker_compose_logs_grep(service: str, pattern: str = '',
                             since: str = '10m', tail: int = 80,
                             root: str = '') -> dict:
    """Строки журнала сервиса за период, отфильтрованные регуляркой.

    Args:
        service: имя сервиса.
        pattern: регулярка; пусто — весь хвост как есть.
        since: период назад в формате docker (`10m`, `1h`, `2006-01-02T15:04:05`).
        tail: не больше этого числа строк в ответе (свежие — последние).
        root: корень проекта.

    Returns:
        {'lines': [...], 'total': строк подошло, 'error': str}.
    """
    cid = _container_id(service, root)
    if not cid:
        return {'lines': [], 'total': 0,
                'error': f'контейнер сервиса «{service}» не найден'}
    lines, err = _logs_since(cid, since, tail=0)
    if err:
        return {'lines': [], 'total': 0, 'error': err}
    if pattern:
        rx = re.compile(pattern)
        lines = [line for line in lines if rx.search(line)]
    return {'lines': lines[-tail:], 'total': len(lines), 'error': ''}


def _container_id(service: str, root: str) -> str:
    """И running-контейнера сервиса; пусто — сервиса в стеке нет."""
    r = subprocess.run(['docker', 'compose', 'ps', '-q', service],
                       capture_output=True, text=True, timeout=20,
                       cwd=root or None)
    return (r.stdout or '').split('\n')[0].strip()


def _logs_since(cid: str, since, tail: int) -> tuple:
    """Журнал контейнера с момента: `since` — epoch-секунда или строка docker.

    Returns:
        `(строки, ошибка)`. Штатный вывод `docker logs` — только stdout:
        сообщения про неверный флаг в stderr — это не строки журнала, а отказ
        читать; смешивать их нельзя, иначе «в журнале usage» выглядит как данные.

    ⚠ Флага `--no-color` у `docker logs` нет — с ним весь вывод превращается
    в usage-подсказку.
    """
    stamp = (since if isinstance(since, str)
             else time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(since)) + 'Z')
    r = subprocess.run(['docker', 'logs', '--since', stamp, cid],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return [], (r.stderr or r.stdout).strip()
    lines = (r.stdout or '').splitlines()
    return (lines if not tail else lines[-tail:]), ''


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Рестарт сервиса с ожиданием готовности и журнал по шаблону.')
    sub = ap.add_subparsers(dest='mode', required=True)
    up = sub.add_parser('restart', help='рестарт и ждать, пока сервис жив')
    up.add_argument('service')
    up.add_argument('--ready', default='', help='регулярка готовности в журнале')
    up.add_argument('--timeout', type=float, default=90.0)
    log = sub.add_parser('logs', help='строки журнала за период')
    log.add_argument('service')
    log.add_argument('--grep', default='', help='регулярка фильтра')
    log.add_argument('--since', default='10m', help='период назад (docker-формат)')
    log.add_argument('--tail', type=int, default=80)
    ns = ap.parse_args()
    if ns.mode == 'restart':
        got = docker_compose_restart_wait(ns.service, ready=ns.ready,
                                          timeout=ns.timeout)
        print(json.dumps(got, ensure_ascii=False))
        raise SystemExit(0 if got['ok'] else 1)
    else:
        got = docker_compose_logs_grep(ns.service, ns.grep, since=ns.since,
                                       tail=ns.tail)
        for line in got['lines']:
            print(line)
        if got['error']:
            print(got['error'], file=sys.stderr)
            raise SystemExit(1)

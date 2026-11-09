"""Журнал systemd-юнита хвостом: последние строки лога, локально и на ферме.

Смотреть, почему сервис умер, — всегда одно и то же: `journalctl -u ... -n 50`
и глазами. Агенту глазами нельзя: вывод journalctl — простыня с timestamp'ами
на сотню строк, а нужен хвост, желательно отфильтрованный и желательно с
того же gx10-2. Явная ловушка дежурного просмотра: `journalctl -f` не
завершается никогда, и вызванный из инструмента процесс висел бы до потолка.

Права на журнал — не по праву рождения: без группы `adm` чужие юниты не
читаются, и это должно быть видно в ответе словом, а не пустым списком —
пустота читается как «в логе чисто».

Спрашивают не всегда юнитом: у чужого демона имя юнита какое угодно, а то и
заведено руками, — зато номер процесса, слушающего порт, виден всегда.
Поэтому выборку сужает либо `unit`, либо `pid` (поле `_PID` journald), либо
оба сразу.
"""

# Хвост по умолчанию — как у `tail`: много для глаза, мало для окна контекста.
SYSTEMD_JOURNAL_LINES = 50

# Потолок короткий: журнал читается с диска, зависнуть ему негде; длинный
# потолок означал бы только сон фермы.
SYSTEMD_JOURNAL_TIMEOUT = 90.0

# До скольки строк тащим при фильтре: журнал мог быть чист весь, а хвост
# после фильтра — пуст; берём с запасом и режем на своей стороне.
SYSTEMD_JOURNAL_SCAN = 5000


def systemd_journal_tail(unit: str = '', *, pid: int = 0,
                         lines: int = SYSTEMD_JOURNAL_LINES,
                         since: str = '', grep: str = '', host: str = '',
                         user: bool = False,
                         timeout: float = SYSTEMD_JOURNAL_TIMEOUT) -> dict:
    """Принести последние строки журнала — своим или с хоста фермы.

    Args:
        unit: имя юнита, экземпляр — с собачкой (`ds-shell@home.service`).
        pid: номер процесса (`_PID` journald) — когда имени юнита нет. Юнит
            чужого демона зовут как угодно, а то и заводят руками, зато
            номер процесса, слушающего порт, виден всегда. С `unit` вместе
            фильтры складываются: строки этого процесса внутри юнита.
        lines: сколько последних строк; при `grep` — после фильтра.
        since: отсечка `--since` как её понимает journalctl (`'1 hour ago'`,
            `'2026-02-19 14:00'`); пусто — без отсечки.
        grep: подстрока, оставляем только строки с ней (фильтр на нашей
            стороне, регистронезависимо).
        host: цель в формате `ssh_x`; пусто — своя машина.
        user: менеджер пользователя (`--user`).
        timeout: потолок секунд; исчерпан — `code` = -1.

    Returns:
        {'code': rc, 'lines': [str], 'error': str}; пустой `lines` при
        `code` = 0 — действительно пусто, при непустом `error` — не пустили
        (нет группы `adm`), юнит молчал или не сказано, чей журнал нужен.

    ⚠ Никакого `-f`: процесс слежения за журналом не завершается сам, и
    вызванный из инструмента висел бы до таймаута, держая канал.

    ⚠ Номера процессов ядро выдаёт по кругу, а журнал помнит всех: выборка
    по `pid` без `since` может принести строки чужой жизни того же номера.
    """
    if not unit and not pid:
        return {'code': -1, 'lines': [], 'error': 'не сказано, чей журнал: ни unit, ни pid'}
    scope = '--user ' if user else ''
    taken = SYSTEMD_JOURNAL_SCAN if grep else max(1, lines)
    target = _target(unit, pid)
    parts = [f'journalctl {scope}-q {" ".join(target)} -n {taken} --no-pager -a']
    if since:
        parts.append(f'--since {shlex_quote(since)}')
    if host:
        from ssh_.ssh_x import ssh_x
        raw = ssh_x(host, ' '.join(parts), timeout=timeout)
    else:
        raw = _local(scope, target, taken, since, timeout)
    kept = [line for line in (raw['output'] or '').splitlines()
            if not grep or grep.lower() in line.lower()]
    return {'code': raw['code'], 'lines': kept[-max(1, lines):], 'error': raw['error']}


# ── детали реализации ──

def _target(unit: str, pid: int) -> list:
    """Чем ограничена выборка: юнит — флагом, процесс — полем journald."""
    return (['-u', unit] if unit else []) + ([f'_PID={int(pid)}'] if pid else [])


def _local(scope: str, target: list, taken: int, since: str, timeout: float) -> dict:
    """Журнал своей машины тем же набором флагов, что и походный скрипт."""
    import subprocess
    cmd = ['journalctl'] + (['--user'] if scope else []) + \
          ['-q'] + target + ['-n', str(taken), '--no-pager', '-a']
    if since:
        cmd += ['--since', since]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return {'code': r.returncode, 'output': r.stdout, 'error': r.stderr}
    except FileNotFoundError:
        return {'code': -1, 'output': '', 'error': 'journalctl не найден'}
    except subprocess.TimeoutExpired:
        return {'code': -1, 'output': '',
                'error': f'journalctl не ответил за {timeout:g} c'}


def shlex_quote(text: str) -> str:
    """Кавычка для походной строки — только для неё, в argv кавычки не нужны."""
    import shlex
    return shlex.quote(text)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='хвост журнала systemd-юнита или процесса; пустые строки '
                    'при code 0 — журнал чист, с error — не пустили.')
    ap.add_argument('unit', nargs='?', default='', help='имя юнита')
    ap.add_argument('--pid', type=int, default=0,
                    help='номер процесса вместо юнита (поле _PID)')
    ap.add_argument('-n', '--lines', type=int, default=SYSTEMD_JOURNAL_LINES,
                    help='строк хвост (после --grep)')
    ap.add_argument('--since', default='', help='отсечка journalctl --since')
    ap.add_argument('-g', '--grep', default='', help='оставить строки с подстрокой')
    ap.add_argument('--host', default='', help='цель в формате ssh_x; пусто — локально')
    ap.add_argument('--user', action='store_true', help='менеджер пользователя')
    ap.add_argument('--timeout', type=float, default=SYSTEMD_JOURNAL_TIMEOUT)
    ns = ap.parse_args()
    out = systemd_journal_tail(ns.unit, pid=ns.pid, lines=ns.lines,
                               since=ns.since, grep=ns.grep, host=ns.host,
                               user=ns.user, timeout=ns.timeout)
    if out['error']:
        print(out['error'])
    for line in out['lines']:
        print(line)
    raise SystemExit(0 if out['code'] == 0 else 1)

"""Состояние systemd-сервиса числом: жив ли, тяжёл ли, каким процессом.

Смотреть на сервис руками (`systemctl status`) для агента бесполезно: вывод
нужно пересказать глазами, а «сколько весит» там — человекочитаемая строка на
выбор версии systemd. Между тем вопрос всегда один и тот же и всегда про
несколько сервисов: жив ли юнит, сколько памяти держит, не рестартует ли без
остановки. Ответы даёт `systemctl show` — машинный формат, где каждое поле
имеет имя, и `NRestarts` там же: сервис, который «работает» и перезапускается
каждую минуту, выглядит живым до первой цифры.

`host` — тот же формат, что у `ssh_x`: поход к ферме ничем не отличается от
локального взгляда, ответ — та же словарь-форма.
"""
import subprocess

# Потолок короткий: `show` не ждёт и не следит, он отвечает с диска сразу;
# длинный потолок значил бы только то, что ферма спит, а мы ждали.
SYSTEMD_STATUS_TIMEOUT = 90.0

# Поля опроса. `NRestarts` — из тех, без чего картина лжёт: «active» сервиса,
# поднявшегося заново в десятый раз, неотличима от живой работы.
SYSTEMD_STATUS_PROPS = ('ActiveState', 'SubState', 'UnitFileState', 'MainPID',
                        'MemoryCurrent', 'NRestarts', 'Description')

# `MemoryCurrent` без значения systemd рисует как -1 беззнаковым числом;
# на GB10 без контрольной группы учёта это обычное дело, а не ноль памяти.
SYSTEMD_MEMORY_UNSET = 18446744073709551615


def systemd_status_get(unit: str, *, host: str = '', user: bool = False,
                       timeout: float = SYSTEMD_STATUS_TIMEOUT) -> dict:
    """Сказать про systemd-юнит: активен ли, тяжёл ли, каким процессом и не рестартует ли.

    Args:
        unit: имя юнита; экземпляр — как есть, с собачкой и угольком
            (`ds-shell@home.service`).
        host: куда смотреть: цель в формате `ssh_x` (`gx10-2`); пусто —
            своя машина.
        user: юнит пользовательского менеджера (`--user`), а не системного.
        timeout: потолок секунд на опрос; исчерпан — `code` = -1.

    Returns:
        {'code': rc, 'state', 'sub', 'enabled', 'pid': int, 'memory': int|None,
        'restarts': int, 'description': str}; `memory` — байты, `None` — юнит
        вне учётной записи (на GB10 — норма). `state`: `active` / `inactive`
        / `failed` / `activating` / `unknown` (юнита нет).

    ⚠ Замороженный сигналом `SIGSTOP` юнит — `active` + `running`, порт
    открыт, память не отдана: по этим полям он живой. Признак заморозки —
    `T` в `/proc/<pid>/stat`, и спрашивать её надо отдельно.
    """
    props = [f'--property={p}' for p in SYSTEMD_STATUS_PROPS]
    scope = '--user ' if user else ''
    if host:
        from ssh_.ssh_x import ssh_x
        script = f'systemctl {scope}show {unit} ' + ' '.join(props)
        raw = ssh_x(host, script, timeout=timeout)
    else:
        cmd = ['systemctl'] + (['--user'] if user else []) + ['show', unit, *props]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=timeout)
            raw = {'code': r.returncode, 'output': r.stdout, 'error': r.stderr}
        except FileNotFoundError:
            raw = {'code': -1, 'output': '', 'error': 'systemctl не найден'}
        except subprocess.TimeoutExpired:
            raw = {'code': -1, 'output': '',
                   'error': f'systemctl не ответил за {timeout:g} c'}
    fields = _parse(raw['output'])
    return {'code': raw['code'],
            'state': fields.get('ActiveState', 'unknown'),
            'sub': fields.get('SubState', 'unknown'),
            'enabled': fields.get('UnitFileState', ''),
            'pid': _number(fields.get('MainPID')),
            'memory': _memory(fields.get('MemoryCurrent')),
            'restarts': _number(fields.get('NRestarts')),
            'description': fields.get('Description', ''),
            'error': raw['error']}


# ── детали реализации ──

def _parse(text: str) -> dict:
    """`Ключ=значение` по строкам; в значении может быть своя `=` (описание)."""
    out = {}
    for line in (text or '').splitlines():
        key, _, value = line.partition('=')
        if _:
            out[key.strip()] = value.strip()
    return out


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _memory(value):
    value = _number(value)
    return None if value in (0, SYSTEMD_MEMORY_UNSET) else value


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='состояние systemd-юнита: state/pid/память/restarts; '
                    'пустой host — своя машина, иначе поход по ssh.')
    ap.add_argument('unit', help='имя юнита, экземпляр — с собачкой')
    ap.add_argument('--host', default='', help='цель в формате ssh_x; пусто — локально')
    ap.add_argument('--user', action='store_true', help='менеджер пользователя (--user)')
    ap.add_argument('--timeout', type=float, default=SYSTEMD_STATUS_TIMEOUT)
    ns = ap.parse_args()
    out = systemd_status_get(ns.unit, host=ns.host, user=ns.user, timeout=ns.timeout)
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0 if out['code'] == 0 else 1)

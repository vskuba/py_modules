"""Файл на другой хост: перенос целиком, права на месте, секретов в `ps` нет.

Руки постоянно тянулись скопировать готовое: обёртку сервиса, конфиг, скрипт
проверки. `ssh_x` умеет везти **скрипт**, но не файл: его stdin занят телом
команды, а похода `scp` в этом слое до сих пор не было вовсе — каждый раз
вспоминался заново и каждый раз с вопросом, как же передать права, чтобы не
танцевать `chmod` отдельным походом.

`ssh_put` везёт файл `rsync`-ом (а где его нет — `scp`-ом), цель та же строка,
что у `ssh_x`: адрес с флагами и ключом одной строкой. Права ставятся тем же
вызовом, если попросить. Ответ — той же формы `{code, output, error}`:
недоступный хост остаётся данными, а не исключением.
"""
import re
import shlex
import subprocess

SSH_PUT_TIMEOUT = 300.0

# Права как их просят: 644, 0755 — ок; `--reference` и прочее сюда не годится.
SSH_PUT_MODE = re.compile(r'^[0-7]{3,4}$')


def ssh_put(src: str, target: str, dest: str, *, mode: str = '',
            timeout: float = SSH_PUT_TIMEOUT) -> dict:
    """Перенести локальный файл на удалённый хост и, если просят, выставить права.

    Args:
        src: локальный файл.
        target: куда — цель в формате `ssh_x`: адрес с флагами и ключом одной
            строкой (`vskuba@gx10-2`, `-i ~/.ssh/k ключ vskuba@gx10-2`).
        dest: путь на том хосте; каталог должен существовать.
        mode: права на перенесённый файл (`'0755'`); пусто — как сложился
            перенос.
        timeout: потолок секунд на перенос и на права вместе.

    Returns:
        {'code': rc, 'output': str, 'error': str} — форма как у `ssh_x`;
        код возврата — часть ответа, таймаут тоже (`code` = -1).

    ⚠ `rsync` предпочтён, но если его нет на **обеих** машинах, поход
    сворачивается на `scp`, а тот не умеет `--chmod` — права тогда ставятся
    отдельным походцем `chmod`, и файл между переносом и правами короткое
    время живёт чужими.
    """
    flags, hostspec = _target_split(target)
    if mode and not SSH_PUT_MODE.match(mode):
        raise ValueError(f'права надо как 644/0755, а не {mode!r}')
    moved = _move(src, flags, hostspec, dest, timeout)
    if moved['code'] != 0:
        return moved
    if not mode:
        return moved
    chmod = _chmod(target, mode, dest, timeout)
    return chmod if chmod['code'] != 0 else moved


# ── детали реализации ──

def _target_split(target: str) -> tuple:
    """Разделить цель `ssh_x` на флаги и `user@host`: флаги — что с минусом."""
    tokens = str(target).split()
    flags = [t for t in tokens if t.startswith('-')]
    hosts = [t for t in tokens if not t.startswith('-')]
    if len(hosts) != 1:
        raise ValueError(f'в цели должен быть ровно один хост, а в {target!r} '
                         f'их {len(hosts)}')
    return flags, hosts[0]


def _move(src: str, flags: list, hostspec: str, dest: str, timeout: float) -> dict:
    """Перенести тело: rsync, а где его нет — scp. Значений в argv секрета нет."""
    remote = f'{hostspec}:{dest}'
    try:
        r = subprocess.run(['rsync', '-a', *flags, '--', src, remote],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout)
    except FileNotFoundError:
        try:
            r = subprocess.run(['scp', *flags, '--', src, remote],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
        except FileNotFoundError:
            return {'code': -1, 'output': '', 'error': 'ни rsync, ни scp не найдены'}
        except subprocess.TimeoutExpired:
            return {'code': -1, 'output': '',
                    'error': f'scp не дошёл за {timeout:g} c'}
    except subprocess.TimeoutExpired:
        return {'code': -1, 'output': '', 'error': f'rsync не дошёл за {timeout:g} c'}
    return {'code': r.returncode, 'output': r.stdout.strip(),
            'error': r.stderr.strip()}


def _chmod(target: str, mode: str, dest: str, timeout: float) -> dict:
    """Выставить права отдельным походцем: тот же транспорт, что у скриптов."""
    from ssh_.ssh_x import ssh_x
    return ssh_x(target, f'chmod {mode} {shlex.quote(dest)}', timeout=timeout)


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='локальный файл на удалённый хост: rsync (fallback scp), '
                    'права тем же вызовом; вывод — JSON {code, output, error}.')
    ap.add_argument('src', help='локальный файл')
    ap.add_argument('target', help='куда: адрес с флагами и ключом одной строкой')
    ap.add_argument('dest', help='путь на том хосте')
    ap.add_argument('--mode', default='', help='права на файле, например 0755')
    ap.add_argument('--timeout', type=float, default=SSH_PUT_TIMEOUT,
                    help='потолок секунд на перенос и права вместе')
    ns = ap.parse_args()
    out = ssh_put(ns.src, ns.target, ns.dest, mode=ns.mode, timeout=ns.timeout)
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0 if out['code'] == 0 else 1)

"""Команда удалённому стеку: скрипт доезжает как есть, кавычки не съедаются.

Ферма — второй компьютер, и до сих пор каждый поход к ней писал заклинание
заново: `ssh хост "docker exec -i контейнер sh -c '...'"`. Кавычки внутри
команды при этом съедаются дважды (ssh-оболочка, потом sh контейнера), grep-
паттерн с `$` или кавычкой долбился на середине, а значения из `.env` светились
в аргументах `ps`. `compose_x` для таких случаев — про ЛОКАЛЬНЫЙ compose-стек:
он подставляет `$ИМЯ` из окружения, но ходит им в свою машину.

`ssh_x` — тот же приём, но к удалённому стеку: скрипт (строка или строки)
уходит на ту машину как есть, через stdin, минуя любой слой кавычек; `args` —
это `{'ИМЯ': '$ПЕРЕМЕННАЯ'}`, имя дописывается в скрипт экспортом, значение
подставляется СЮДА из окружения и наружу флагом не уезжает вовсе. Шум
сессии (`Warning: Permanently added…`) из вывода выбрасывается.
"""
import os
import shlex
import subprocess


def ssh_x(target: str, script, *, container: str = '', args: dict = None,
          quiet=('Warning: Permanently added',)) -> dict:
    """Выполнить скрипт на удалённом хосте (или в контейнере его стека).

    Скрипт уходит как есть, через stdin: никакие кавычки по дороге не
    съедаются, значения не светятся в списке процессов.

    Args:
        target: куда ходить — `пользователь@хост` с флагами и ключом одной
            строкой (как `photo_train_ssh_target()`): строка делится пробелами
            и уходит в argv как есть.
        script: скрипт: строка или список строк; доезжает как есть, stdin-ом.
        container: контейнер стека на том хосте (`comfyui-gb10`); пусто — скрипт
            выполняет сам хост.
        args: {'ИМЯ': 'значение'} — дописываются в начало скрипта экспортом;
            значение `'$ИМЯ'` берётся из локального окружения (`.env`).
        quiet: подстроки-шум, такие строки вывода выбрасываются.

    Returns:
        {'код': rc, 'вывод': stdout без шума, 'ошибка': stderr без шума};
        код возврата — часть ответа, а не исключение.
    """
    head = []
    for name, value in (args or {}).items():
        val = str(value)
        if val.startswith('$'):
            val = os.environ.get(val[1:], '')
        head.append(f'export {name}={shlex.quote(val)}')
    body = script if isinstance(script, str) else '\n'.join(script)
    stdin_data = '\n'.join(head + [body])
    remote = f'docker exec -i {container} sh -s' if container else 'sh -s'
    r = subprocess.run(['ssh', *str(target).split(), remote], input=stdin_data,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    return {'код': r.returncode, 'вывод': _quiet(r.stdout, quiet),
            'ошибка': _quiet(r.stderr, quiet)}


def _quiet(text: str, marks) -> str:
    return '\n'.join(line for line in text.splitlines()
                     if not any(m in line for m in marks))


if __name__ == '__main__':
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(
        description='скрипт удалённому стеку как есть, через stdin; вывод — '
                    'JSON без шума (--no-quiet не гасить).')
    ap.add_argument('target', help='куда ходить: адрес с флагами и ключом '
                                   'одной строкой')
    ap.add_argument('script', help='скрипт; «-» — прочитать со stdin')
    ap.add_argument('--container', default='', help='контейнер стека на том '
                                                    'хосте')
    ap.add_argument('--var', action='append', default=[], metavar='ИМЯ=значение',
                    help='экспорт в начало скрипта; значение $ИМЯ берётся из '
                         'окружения; повторять')
    ap.add_argument('--no-quiet', action='store_true',
                    help='не выбрасывать шумовые строки из вывода')
    ns = ap.parse_args()
    body = sys.stdin.read() if ns.script == '-' else ns.script
    print(json.dumps(ssh_x(ns.target, body, container=ns.container,
                           args=dict(v.split('=', 1) for v in ns.var),
                           quiet=() if ns.no_quiet else
                           ('Warning: Permanently added',)),
                     ensure_ascii=False))

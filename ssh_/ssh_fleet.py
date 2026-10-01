"""Одна команда нескольким хостам разом: флот отвечает словарём по алиасам.

Ферма — это не один хост, а два и больше, и опрос их всегда парный: «что там
на gx10-2 и gx10-4». Без этого модуля каждый опрос писался циклом по шеллу,
цикл глотал ошибки машины, вставшей на ровном месте, а ответы двух хостов
сливались в одну простыню, где не разобрать, где чьё.

`ssh_x` — примитив похода: скрипт доезжает через stdin как есть. `ssh_fleet_run`
кладёт поверх него параллельность (threads, порядок выдачи не важен — ответ
ключуется алиасом) и главное: **нечитаемый хост — запись в ответе, а не
исключение**. Ферма, ушедшая в сон, не должна срывать опрос остальных.
"""
from concurrent.futures import ThreadPoolExecutor

from ssh_.ssh_x import ssh_x

# Потолок похода тот же, что у одиночного `ssh_x`: сонная машина обязана быть
# вычеркнута за разумное время, а не вешать опрос флота до TCP-таймаута ядра.
SSH_FLEET_TIMEOUT = 900.0

# Потолок параллельности: хостов в флотах пока единицы, но держать тред на
# каждого — привычка из времен, когда потоки были дешёвыми.
SSH_FLEET_PARALLEL = 8


def ssh_fleet_run(hosts, script, *, container: str = '', args: dict = None,
                  timeout: float = SSH_FLEET_TIMEOUT) -> dict:
    """Выполнить один скрипт на нескольких хостах параллельно — ответ по алиасам.

    Args:
        hosts: куда ходить — итерируемое целей в формате `ssh_x` (`vskuba@gx10-2`,
            алиас из `~/.ssh/config`, цель с флагами и ключом одной строкой).
        script: скрипт: строка или список строк; доезжает каждому как есть,
            stdin-ом — кавычки не съедаются.
        container: контейнер стека на каждом хосте; пусто — скрипт выполняет
            сам хост.
        args: {'ИМЯ': 'значение'} — экспортируются в начало скрипта; значение
            `'$ИМЯ'` берётся из локального окружения.
        timeout: потолок секунд на каждый хост; исчерпан — `code` = -1.

    Returns:
        {алиас: {'code', 'output', 'error'}} — форма ответа ровно как у
        `ssh_x`, только обёрнутая ключом хоста. Мёртвый хост — запись с
        `code` = -1 наравне с живыми.

    ⚠ Ключ ответа — **строка цели как её передали**, а не имя хоста из неё:
    `vskuba@gx10-2` и `gx10-2` — два ключа на одну машину. Называй цели
    одинаково, иначе не найдёшь ответ своего же хоста.
    """
    names = [str(h) for h in hosts]
    with ThreadPoolExecutor(max_workers=max(1, min(len(names), SSH_FLEET_PARALLEL))) as pool:
        futures = {name: pool.submit(_one, name, script, container, args, timeout)
                   for name in names}
        return {name: fut.result() for name, fut in futures.items()}


# ── детали реализации ──

def _one(target: str, script, container: str, args: dict, timeout: float) -> dict:
    """Поход к одному хосту: любая его ошибка сворачивается в запись-ответ."""
    try:
        return ssh_x(target, script, container=container, args=args, timeout=timeout)
    except Exception as error:            # сонный хост не должен срывать остальных
        return {'code': -1, 'output': '', 'error': f'{type(error).__name__}: {error}'}


if __name__ == '__main__':
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(
        description='один скрипт нескольким хостам параллельно; вывод — JSON '
                    '{алиас: {code, output, error}}; код возврата — число '
                    'неответивших хостов.')
    ap.add_argument('hosts', nargs='+', help='цели в формате ssh_x, повторять')
    ap.add_argument('script', help='скрипт; «-» — прочитать со stdin')
    ap.add_argument('--container', default='', help='контейнер стека на каждом')
    ap.add_argument('--var', action='append', default=[], metavar='ИМЯ=значение',
                    help='экспорт в начало скрипта; повторять')
    ap.add_argument('--timeout', type=float, default=SSH_FLEET_TIMEOUT,
                    help='потолок секунд на каждый хост')
    ns = ap.parse_args()
    body = sys.stdin.read() if ns.script == '-' else ns.script
    answers = ssh_fleet_run(ns.hosts, body, container=ns.container,
                            timeout=ns.timeout,
                            args=dict(v.split('=', 1) for v in ns.var))
    print(json.dumps(answers, ensure_ascii=False, indent=1))
    raise SystemExit(sum(1 for a in answers.values() if a['code'] != 0))

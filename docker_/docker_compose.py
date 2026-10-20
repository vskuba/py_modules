"""Python-код этим интерпретатором, что и сервис стека: одной строкой снаружи.

Разовый вызов доменной функции обычно выглядит так: `docker compose exec -T
<сервис> python - <<'PY'`, и дальше шапка импортов, `asyncio.run(...)` руками,
`print(...)`, а без `2>/dev/null` вывод тонет в предупреждениях docker. На
каждый такой раз — пять строк обвязки, и она же каждый раз источник опечатки.

`compose_x` умеет давать контейнеру CLI-команду с подстановкой учётки из `.env`,
но не код: у неё `argv`, а не stdin. Здесь — то же самое для Python: кусок кода
уходит на stdin интерпретатора сервиса, обёртка `asyncio.run` добавляется сама,
а `return` значения печатается JSON'ом — результат читается, а не разбирается
из мешанины print'ов.

В отличие от `project_probe` (тот исполняет код на хосте, подменяя адреса
контейнера внешними), этот инструмент исполняет код ВНУТРИ контейнера: теми же
путями, теми же библиотеками образа и тем же сетевым кругом. Когда код трогает
файлы тома или ходит по именам сервисов — наружу он не переносится.
"""
import argparse
import subprocess
import sys

# Обёртка над пользовательским кодом. Пять её строк — это постоянный сдвиг
# нумерации: в трейсбеке номер строки = строка куска + 5, и это лучше рваных
# пустых строк, которые поехали бы от порядка вставки (см. `code_rules`, про
# понятную арифметику вместо магии).
_WRAP_HEAD = ('import asyncio\nimport json as _json\n\n\n'
              'async def _docker_compose_code():\n')
_WRAP_TAIL = ('\n\n\n_got = asyncio.run(_docker_compose_code())\n'
              'if _got is not None:\n'
              '    print(_json.dumps(_got, ensure_ascii=False, default=str))\n')


def docker_compose_python(service: str, code: str, *, timeout: float = 120.0,
                          root: str = '') -> dict:
    """Выполнить Python-код интерпретатором сервиса compose-стека.

    Args:
        service: имя сервиса (`cyber_worker`, `persona_uvicorn`, …).
        code: программа. Пишется так, будто она уже внутри корутины: `await`
            разрешён, а возвращаемое значение (`return {...}`) печатается
            JSON'ом; обычный `print` тоже доезжает.
        timeout: потолок в секундах; исчерпан — `code` = -1 и слово в `error`.
        root: корень проекта с `docker-compose.yml`; пусто — текущий каталог.

    Returns:
        {'code': rc, 'output': stdout, 'error': stderr}.

    ⚠ Таймаут и «сервис не поднят» — такой же ответ, как ненулевой код возврата
    процесса, а не исключение: `compose_x` держит то же правило, и зов из
    цикла не должен обрываться на первом упавшем контейнере.

    ⚠ Трейсбек изнутри куска съезжает на 5 строк (размер шапки обёртки), и
    этот сдвиг постоянен.

    ⚠ Классы и `def` внутри куска работают, но имя куска — локальное для
    корутины: на код сверху из `__main__` он не походит. Это цена переносимых
    номеров строк в трейсбеке; ради одной функции радиуса действия не хватает.
    """
    body = '\n'.join('    ' + line for line in code.splitlines()) or '    pass'
    source = _WRAP_HEAD + body + _WRAP_TAIL
    try:
        r = subprocess.run(['docker', 'compose', 'exec', '-T', service,
                            'python', '-'],
                           input=source, capture_output=True, text=True,
                           timeout=timeout, cwd=root or None)
    except subprocess.TimeoutExpired:
        return {'code': -1, 'output': '',
                'error': f'не ответил за {timeout:g} c: сервис «{service}» '
                         f'не поднят или код не завершается сам'}
    except FileNotFoundError:
        return {'code': -1, 'output': '', 'error': 'docker не найден в PATH'}
    return {'code': r.returncode, 'output': r.stdout, 'error': r.stderr}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Python-код внутри контейнера сервиса: без heredoc, '
                    'asyncio.run и шапок импортов.')
    ap.add_argument('service', help='имя сервиса compose-стека')
    ap.add_argument('code', nargs='?', default='',
                    help='программа; «-» — прочитать со stdin')
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument('--root', default='', help='корень проекта с compose-файлом')
    ns = ap.parse_args()
    source = ns.code
    if source in ('-', ''):
        source = sys.stdin.read()
    got = docker_compose_python(ns.service, source, timeout=ns.timeout,
                                root=ns.root)
    sys.stdout.write(got['output'])
    if got['error']:
        sys.stderr.write(got['error'])
    sys.exit(got['code'] if got['code'] >= 0 else 1)

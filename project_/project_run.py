"""
Разовый Python в окружении проекта — вместо `cd … && .venv/bin/python - <<'EOF'`.

Заклинание из заголовка агент пишет по десять раз за сессию и ошибается в нём
тремя способами: не тот каталог (импорт проекта не находится), системный
интерпретатор вместо `.venv` (не находятся зависимости), пустой `PYTHONPATH`
(не находится `py_modules`). Симптом у всех трёх один — `ModuleNotFoundError`
на первой строке, и полный круг модели уходит на то, чтобы его прочитать.

Здесь это один вызов, который берёт каталог, интерпретатор и пути из
`project_` — то есть от места самого модуля, а не от того, где стоял агент.

Отдельно про `--async`: половина библиотеки асинхронная, и обёртка
`asyncio.run(_main())` в сниппете — ещё одна строка, которую пишут наизусть и
забывают. Флаг делает её сам, тело едет как есть.
"""
import argparse
import subprocess
import sys
from pathlib import Path

# Файл запускают путём (`python3 py_modules/project_/project_run.py`) — тогда в
# путях лежит каталог файла, а не `py_modules`, и соседний модуль не виден. Свой
# каталог при этом ещё и мешает: обычный модуль в нём побеждает одноимённый пакет
# без `__init__.py` из `py_modules`. Поэтому его убираем, а не просто дописываем путь.
# Импортированному модулю (`__package__` заполнен) правка путей не нужна и вредна.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_.project_ import project_env, project_python, project_root

# Потолок по умолчанию для CLI. Ноль — без ограничения; ставится осознанно, потому
# что зависший разовый скрипт (забытый `await`, недоступная база) держит круг до
# таймаута вызывающего и не говорит о себе ничего.
PROJECT_RUN_DEFAULT_TIMEOUT = 300.0


def project_run_python(source: str = '', path: str = '', module: str = '',
                       argv: list | None = None, is_async: bool = False,
                       timeout: float = 0) -> int:
    """
    Запустить код, файл или модуль интерпретатором проекта из корня проекта.

    Ровно один из `source`, `path`, `module` — что запускать. Вывод не
    перехватывается: он идёт в те же потоки, что у вызывающего, чтобы длинный
    скрипт было видно по ходу дела.

    Args:
        source: текст программы (как `python -c`).
        path: путь к файлу; относительный считается от корня проекта.
        module: имя модуля (как `python -m`) — так запускают pytest, alembic и прочее.
        argv: аргументы программе, они же `sys.argv[1:]`.
        is_async: обернуть `source` в `async def` и `asyncio.run` — тело пишется
            так, будто оно уже внутри корутины.
        timeout: секунды; 0 — без ограничения.

    Returns:
        Код возврата процесса; 124 — не уложился в `timeout` (как у `timeout(1)`).

    Raises:
        ValueError: не назван или назван больше чем один источник кода.
    """
    named = [bool(source), bool(path), bool(module)]
    if sum(named) != 1:
        raise ValueError('нужен ровно один источник: код, файл или модуль')

    root = project_root()
    if source:
        command = ['-c', _async_wrap(source) if is_async else source]
    elif module:
        command = ['-m', module]
    else:
        target = Path(path)
        command = [str(target if target.is_absolute() else root / target)]

    done = ['-u', *command, *[str(item) for item in (argv or [])]]
    try:
        return subprocess.run([str(project_python()), *done], cwd=str(root),
                              env=project_env(), timeout=timeout or None).returncode
    except subprocess.TimeoutExpired:
        print(f"project_run: не уложился в {timeout:g} с — процесс снят", file=sys.stderr)
        return 124


# ── детали реализации ──

def _async_wrap(source: str) -> str:
    """Тело корутины -> самостоятельная программа: `async def _main()` и `asyncio.run`."""
    body = '\n'.join('    ' + line for line in source.splitlines()) or '    pass'
    return 'import asyncio\n\n\nasync def _main():\n' + body + '\n\n\nasyncio.run(_main())\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Разовый Python в окружении проекта: свой каталог, свой .venv, свой PYTHONPATH.',
        epilog="примеры: -c 'import sys; print(sys.path)' | -a 'print(await f())' | "
               "-m pytest tests/ | script.py arg | - (код со stdin)")
    parser.add_argument('target', nargs='?', default='',
                        help="файл со скриптом или `-` — читать код со stdin")
    parser.add_argument('-c', '--code', default='', help='текст программы')
    parser.add_argument('-a', '--async-code', default='',
                        help='то же, но тело корутины: обёртка asyncio.run добавляется сама')
    parser.add_argument('-m', '--module', default='', help='запустить модуль, как `python -m`')
    parser.add_argument('-t', '--timeout', type=float, default=PROJECT_RUN_DEFAULT_TIMEOUT,
                        help=f'секунды, 0 — без ограничения (по умолчанию {PROJECT_RUN_DEFAULT_TIMEOUT:g})')
    parser.add_argument('args', nargs=argparse.REMAINDER, help='аргументы программе')
    args = parser.parse_args()

    code, is_async = args.code, False
    if args.async_code:
        code, is_async = args.async_code, True
    if args.target == '-':
        code = sys.stdin.read()

    file_path = '' if args.target in ('', '-') else args.target
    # `-m pytest tests/` и `-c '...' arg`: argparse отдаёт первое слово после
    # источника позиционной цели, а не в REMAINDER — она объявлена раньше и
    # берёт своё первой. Значит, при названном источнике цель — уже аргумент
    # программе, а не файл со скриптом.
    if file_path and (code or args.module):
        args.args = [file_path] + list(args.args)
        file_path = ''
    try:
        raise SystemExit(project_run_python(source=code, path=file_path, module=args.module,
                                            argv=args.args, is_async=is_async,
                                            timeout=args.timeout))
    except ValueError as err:
        raise SystemExit(f"ошибка: {err}")

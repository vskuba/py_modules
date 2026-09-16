"""
Сюита проекта одной строкой: тот же интерпретатор, что у панели, и весь вывод.

Гонять сюиту руками (`cd`, `pytest -k`, `.venv` не тот, интерпретатор не тот)
значит ловить провалы «не там запустил»: pytest без PYTHONPATH общего слоя
ломается на первом же импорте, системная python'а не знает про venv проекта.
Между тем прогон сюиты — это ровно `project_run_python(module='pytest', ...)`:
тот же интерпретатор, что поднимает панель, корень проекта, PYTHONPATH с
общим слоем.

Здесь только то, чего не хватает прогону: выбор цели, фильтрация `-k`/`-m`,
соблюдение кода возврата. Всё остальное делает `project_run`.
"""
import argparse
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_.project_run import project_run_python


def project_test(target: str = 'tests/', k: str = '', marker: str = '',
                 extra: list | None = None, timeout: float = 300) -> int:
    """Прогнать сюиту (или её кусок) и вернуть код возврата.

    Args:
        target: путь или узел pytest (`tests/test_photo_search.py::test_x`).
        k: фильтр `-k` (подстрока имён тестов).
        marker: фильтр `-m` (например `slow`).
        extra: прочие аргументы pytest (`['-x', '-v']`).
        timeout: секунды; 0 — без ограничения.

    Returns:
        Код возврата pytest (0 — всё зелёное, 124 — timeout).
    """
    argv = [target]
    if k:
        argv += ['-k', k]
    if marker:
        argv += ['-m', marker]
    argv += list(extra or [])
    return project_run_python(module='pytest', argv=argv, timeout=timeout)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Сюита проекта: тот же интерпретатор, что у панели.')
    ap.add_argument('target', nargs='?', default='tests/',
                    help='путь или узел pytest')
    ap.add_argument('--k', default='', help='фильтр -k')
    ap.add_argument('--m', default='', help='фильтр -m (маркер)')
    ap.add_argument('extra', nargs='*', default=[], help='прочие аргументы pytest')
    ns = ap.parse_args()
    raise SystemExit(project_test(ns.target, ns.k, ns.m, ns.extra))

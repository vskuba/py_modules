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
import re
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_.project_ import project_git, project_root
from project_.project_run import project_run_python

# Примета метки в файле сюиты: берут и из `pytest.mark.<имя>`, и из модульного
# `pytestmark = pytest.mark.<имя>` — форма одна. `parametrize` — не метка запуска,
# а способ раскладки теста: в отчёте о группах прохода он только шумит.
PROJECT_TEST_MARK = re.compile(r'pytest\.mark\.(\w+)')
PROJECT_TEST_NOT_MARK = ('parametrize',)


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


def project_test_scope(modules: tuple = (), root: str = '',
                       test_dirs: tuple = ()) -> list[dict]:
    """Какие сюиты идут под изменённые модули: тесты, что зовут эти имена.

    Выбор цели был единственной частью круга, что жила в доке и в памяти:
    «тронул модуль — бегай его группы» каждый раз читалось заново. Здесь тот же
    выбор становится строкой кода: модуль — ищем тесты, что импортируют его или
    подменяют его имена; метки (`llm`, `chat`) вытаскиваем из самих файлов,
    роль решает, что из них запускать руками.

    Args:
        modules: изменённые пути; пусто — берём `git diff HEAD` целиком.
        root: корень проекта; пусто — от места модуля.
        test_dirs: где искать тесты; пусто — все верхние каталоги с тестовым
            началом (`tests/`, `tests_llm/`, ...) — их знает pytest, не мы.

    Returns:
        list[dict]: `{module, tests, markers}` — модуль, найденные файлы сюит
        (пути от корня) и метки этих файлов. Пусто — под правку тестов нет.

    ⚠ Тест считается затронутым по слову имени в файле — тот же текстовый круг,
    что у `tool_dead`: подмена строкой (`monkeypatch.setattr`) здесь живой тест,
    а не шум; решения о запуске `llm`/`llm_live` остаются за человеком.
    """
    base = Path(root or project_root()).resolve()
    if not modules:
        modules = tuple(filter(None, project_git(base, 'diff', '--name-only',
                                                 'HEAD').splitlines()))
    if not test_dirs:
        test_dirs = tuple(sorted(p.name for p in base.iterdir()
                                 if p.is_dir() and p.name.startswith('test')))

    out = []
    for one in modules:
        # Имя модуля — последнее слово пути: `src/dating/answer.py` зовут в
        # тестах словом `answer` (import, setattr), и этим же словом их ищем.
        token = Path(one).stem if Path(one).suffix else Path(one).name
        if token.startswith('test_'):         # изменён сам тест — он и есть цель
            out.append({'module': one, 'tests': [one],
                        'markers': _markers(base / one)})
            continue

        word = re.compile(rf'(?<!\w){re.escape(token)}(?!\w)')
        hit = []
        for d in test_dirs:
            for p in (base / d).rglob('*.py'):
                if word.search(p.read_text(encoding='utf-8', errors='replace')):
                    hit.append(str(p.relative_to(base)).replace('\\', '/'))
        if hit:
            markers = sorted(set().union(*(_markers(base / p) for p in hit)))
            out.append({'module': one, 'tests': hit, 'markers': markers})

    return out


# ── детали реализации ──

def _markers(path: Path) -> set:
    """Метки запуска сюиты файла: `pytest.mark.<имя>` и `pytestmark`."""
    try:
        return {one for one in PROJECT_TEST_MARK.findall(
            path.read_text(encoding='utf-8', errors='replace'))
            if one not in PROJECT_TEST_NOT_MARK}
    except OSError:
        return set()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Сюита проекта: тот же интерпретатор, что у панели.')
    ap.add_argument('target', nargs='?', default='',
                    help='путь или узел pytest')
    ap.add_argument('--k', default='', help='фильтр -k')
    ap.add_argument('--m', default='', help='фильтр -m (маркер)')
    ap.add_argument('--scope', action='store_true',
                    help='не гонять: показать сюиты под изменённые модули '
                         '(пусто — весь дифф HEAD)')
    ap.add_argument('extra', nargs='*', default=[], help='прочие аргументы pytest')
    ns = ap.parse_args()
    if ns.scope:
        found = project_test_scope((ns.target,) + tuple(ns.extra)
                                   if ns.target else ())
        for entry in found:
            print(f"── {entry['module']}")
            for t in entry['tests']:
                print(f'   {t}')
            if entry['markers']:
                print(f"   метки: {', '.join(entry['markers'])}")
        raise SystemExit(0)
    raise SystemExit(project_test(ns.target or 'tests/', ns.k, ns.m, ns.extra))

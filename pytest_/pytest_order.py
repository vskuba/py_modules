"""Провал от порядка, а не от правки: каждый упавший тест — отдельно.

Правя общий скелет (круги, делегаты, хуки), легко получить «красную группу»,
которая к правке отношения не имеет: тесты делят модульное состояние —
отметку темпа в памяти процесса, кольцо ленты, — и в общем прогоне кто-то
приходит вторым туда, где первому было зелёно. Замерено на одной сессии:
две ложные волны заходов, обе лечились «изолированный перезапуск зелёный —
винить не правку», и каждый раз это были ручные вызовы.

## Что делает

`pytest_order(вывод_прогона)` — берёт строки `FAILED ...::test_x` из вывода
`pytest`, перезапускает каждый упавший узел **один** тем же интерпретатором и
делит на два списка: «валивается и один» (правка виновата) и «один зелёный»
(плывёт от порядка или общего состояния).

## ⚠ Это разбор прогона, не новый сторож

Среду тестов он не чинит и не меняет: окружение то, в котором запущен, —
проект с `.env` надо запускать уже с ним. И «один зелёный» не значит «тест
плохой»: значит «это не твоя правка, ищи соседа по состоянию».
"""
import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PYTEST_ORDER_FAILED_RE = re.compile(r'^FAILED (\S+::\S+)', re.M)


def pytest_order(report: str, extra: tuple = ()) -> list:
    """Каждый упавший тест — изолированно: правка виновата или порядок.

    Args:
        report: вывод прогона с `FAILED ...` строками (обычно `pytest -q`).
        extra: дополнительные аргументы pytest (`('-m', 'chat')`).

    Returns:
        Список `{'nodeid', 'alone_ok'}`: `alone_ok=True` — один зелёный,
        провал пришёл от порядка/общего состояния; `False` — валится и один.

    Raises:
        RuntimeError: в выводе нет ни одного упавшего узла — нечего разбирать.
    """
    nodeids = PYTEST_ORDER_FAILED_RE.findall(report)

    if not nodeids:
        raise RuntimeError('в выводе нет ни одной строки FAILED — нечего разбирать')

    out = []

    for nodeid in dict.fromkeys(nodeids):  # порядок без повторов
        proc = subprocess.run(
            [sys.executable, '-m', 'pytest', nodeid, '-q', '--no-header', '-p', 'no:cacheprovider', *extra],
            capture_output=True, text=True, timeout=300)
        out.append({'nodeid': nodeid, 'alone_ok': proc.returncode == 0})

    return out


def pytest_order_format(rows: list) -> str:
    """Отчёт словами: сначала виновники правки, потом жертвы порядка."""
    blame = [r for r in rows if not r['alone_ok']]
    order = [r for r in rows if r['alone_ok']]

    lines = ['валятся и в одиночку — винить правку:' if blame else 'одиночных провалов нет, правка чиста']
    lines += [f"   {r['nodeid']}" for r in blame]
    lines.append('')
    lines.append('один зелёный — провал пришёл от порядка/общего состояния:'
                 if order else 'жертв порядка нет')
    lines += [f"   {r['nodeid']}" for r in order]
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Что из упавшего валится и в одиночку — а что приползло от порядка.')
    parser.add_argument('report', help='файл с выводом прогона; «-» — из stdin')
    parser.add_argument('-m', dest='marks', action='append', default=[],
                        help='метки, как в основном прогоне (повторяемо)')
    args = parser.parse_args()

    try:
        text = sys.stdin.read() if args.report == '-' else Path(args.report).read_text(encoding='utf-8')
        extra = tuple(x for m in args.marks for x in ('-m', m))
        print(pytest_order_format(pytest_order(text, extra)))
    except (RuntimeError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')

    raise SystemExit(0)

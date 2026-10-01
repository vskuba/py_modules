"""
Новая страница — три места; целые ли они, одной командой.

Точка в роутере, шаблон, подсветка в навигации: забытое место молчит — страница
ездит без подсвеченного раздела, роутер ссылается на шаблон, которого нет.
Функция смотрит на все три сразу и отвечает по каждой странице: где дыра.
Про конкретный проект она не знает ничего — пути к роутерам, шаблонам и
навигации передают ей; годится любой FastAPI-проект с map'ом разделов в шаблоне.
"""
import argparse
import re
import sys

from pathlib import Path

# Файл запускают и путём (`python3 py_modules/page_/page_wire.py`). Тогда первым в путях
# лежит каталог файла, и соседний namespace (`file_`) не находится вовсе.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_.file_walk import file_walk

# decorator с URL → литерал шаблона внутри функции (обычно через хелпер
# _dashboard_page(request, user, 'admin/x.html') — важен сам литерал).
# окно {0,600}: между декоратором и литералом — тело одной функции, не вся папка.
_ROUTE = re.compile(r'@\w+\.(?:get|post|put|delete)\(\s*[\'"]([^\'"]+)[\'"]'
                    r'[\s\S]{0,600}?[\'"]([\w/.\-]+\.html)[\'"]')


def page_wire_check(routers, template_dir, nav_file) -> list[dict]:
    """Страницы, которые роутеры отдают шаблонами, против шаблонов и навигации.

    Args:
        routers: файл или каталог с роутерами (список тоже годится).
        template_dir: каталог шаблонов.
        nav_file: файл навигации (в проектах — шапка с картой разделов).

    Returns:
        [{'url', 'template', 'file': bool, 'nav': bool}, ...] — по странице
        на каждый литерал шаблона в роутере; дыра там, где False.
    """
    roots = [Path(r) for r in (routers if isinstance(routers, (list, tuple))
                               else [routers])]
    py_files = [p for root in roots for p in file_walk(root, ('.py',))]
    nav_text = Path(nav_file).read_text(encoding='utf-8')
    out = []
    for rf in py_files:
        if rf.suffix != '.py':
            continue
        for url, name in _ROUTE.findall(rf.read_text(encoding='utf-8')):
            out.append({'url': url, 'template': name,
                        'file': (Path(template_dir) / name).exists(),
                        'nav': (f"'{url}'" in nav_text) or (f'"{url}"' in
                                                           nav_text)})
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Три места новой страницы: точка, шаблон, навигация.')
    ap.add_argument('routers', help='файл/каталог роутеров через запятую')
    ap.add_argument('--templates', default='templates', help='каталог шаблонов')
    ap.add_argument('--nav', required=True, help='файл навигации')
    ns = ap.parse_args()
    try:
        for row in page_wire_check(ns.routers.split(','), ns.templates, ns.nav):
            mark = '✓' if row['file'] and row['nav'] else '✗'
            print(f"{mark} {row['url']:26} {row['template']:26} "
                  f"шаблон={'✓' if row['file'] else 'НЕТ':4} "
                  f"навигация={'✓' if row['nav'] else 'НЕТ'}")
    except (RuntimeError, ValueError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')

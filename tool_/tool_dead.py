"""
Имена, которые определены, но никто их не читает: мёртвый код списком.

    PYTHONPATH=py_modules python -m tool_.tool_dead src
    PYTHONPATH=py_modules python -m tool_.tool_dead . --private   # только приватные

Мёртвый код не мешает работать — он мешает читать. Функция с докстрингом, обещающим
«одно написание имени на три места», выглядит частью устройства, и следующий читатель
строит на ней рассуждение; а зовущих у неё нет ни одного, и обещание давно не
выполняется. Дважды за одну сессию так и было: `_history_text` остался от снятого
механизма истории, `mail_agent_name` обещал единое имя агента, которое `_agent_id`
рядом собирал сам.

Найти это глазами нельзя — имён в проекте сотни. `grep` по одному имени отвечает, но
спрашивать его надо про каждое.

## ⚠⚠ Использование ищется текстом, а не графом вызовов

Граф вызовов был бы точнее и оказался бы негодным: в живом проекте 476 обращений
через `getattr`/`setattr`, и тесты подменяют даже приватные имена строкой —
`monkeypatch.setattr(answer, '_agent_ask', _ask)`. Граф назвал бы такое мёртвым, а
оно — то самое место, где имя обязано совпадать.

Поэтому вопрос ставится иначе: **встречается ли имя где-нибудь, кроме своего
определения**. Ответ по всем слоям — код, разметка, тесты, доки, — как у
`tool_impact`.

## ⚠⚠ Комментарии и докстринги вычёркиваются

Иначе мёртвое прячется за упоминанием себя же: у `_number_check` в том же файле
стоял комментарий «решает сервер (`_number_check`)» — одно это упоминание выдало бы
живое имя. Вычёркиванием заняты готовые `pytest_guard_code` (питон, разбором) и
`pytest_guard_text` (прочее, вычёркиванием).

## ⚠⚠ Что каркас зовёт сам — не мёртвое

Обработчик `@router.get`, фикстура, тест, `main`, `__init__` зовутся **не по имени**:
их берёт FastAPI, pytest, оболочка. В живом проекте таких обработчиков 176 — не
исключи их, и отчёт станет бесполезным с первой строки (`TOOL_DEAD_ALIVE`).

## ⚠⚠ В общем слое публичные имена судить нельзя

`py_modules` подключён submodule-ом в несколько проектов, и его функции зовут
**оттуда**. «Ни одного зова здесь» для публичного имени слоя не значит ничего —
значит лишь, что его не зовут внутри самого слоя.

Отсюда два режима: в проекте судят всё, в библиотеке — только приватные
(`--private`). Режим выбирается словом, а не угадывается: угадать нельзя, а
ошибиться — значит предложить удалить чужой рабочий вход.

## Три вердикта, и только первый — про удаление

| Вердикт | Что значит |
|---------|------------|
| `мёртвое` | не встречается больше нигде |
| `только тесты` | код живёт ради собственного теста |
| `только док` | упомянуто в доке, кодом не зовётся — расходится док или код |

⚠ И это **кандидаты**: имя может приезжать из базы, из workflow соседа или из
шаблона, которого нет в дереве. Решает человек.
"""
import ast
import re
import sys

from pathlib import Path

# Файл запускают и путём. Тогда первым в путях лежит каталог файла, и соседний
# namespace (`file_`, `pytest_`) не находится вовсе.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_.file_walk import file_walk
from pytest_.pytest_guard import pytest_guard_code, pytest_guard_text
from tool_.tool_impact import TOOL_IMPACT_SUFFIXES

# Слои ответа — те же, что у `tool_impact`: отчёты двух инструментов про одно и то же
# имя должны читаться одинаково.
TOOL_DEAD_TESTS = 'тесты'
TOOL_DEAD_DOC = 'док'

# Вердикты. ⚠ Про удаление говорит только первый; остальные — про расхождение.
TOOL_DEAD_GONE = 'мёртвое'
TOOL_DEAD_TESTS_ONLY = 'только тесты'
TOOL_DEAD_DOC_ONLY = 'только док'

# Имена, которые каркас зовёт сам. ⚠⚠ Без этого списка отчёт начинается со 176
# обработчиков FastAPI — см. ⚠⚠ в докстринге модуля.
TOOL_DEAD_ALIVE = ('main', 'lifespan')

# Приметы входной точки в декораторах: маршрут, фикстура, свойство, переходник.
#
# ⚠⚠ Сверяются с `ast.unparse`, то есть с **исходным видом** декоратора, а не с
# `ast.dump`. В дампе `@router.get('/x')` выглядит как `Attribute(value=Name(id=
# 'router'…), attr='get'…)` — подстроки `router.` там нет вовсе, и первая версия не
# отсеивала ни одного обработчика, хотя список примет был верный.
TOOL_DEAD_ALIVE_MARKS = ('router.', 'app.', 'fixture', 'property', 'setter',
                         'staticmethod', 'classmethod', 'overload', 'hookimpl',
                         'celery', 'task', 'cron', 'listener', 'command')

# Начала имён, которые каркас зовёт сам.
TOOL_DEAD_ALIVE_STARTS = ('test_', '__')

# Ветки, которые в обход не берутся: чужой код и черновики.
#
# ⚠⚠ Сверяется путь **от корня обхода**, а не абсолютный. С абсолютным инструмент
# отсеивал всё дерево целиком, стоило проекту лежать под каталогом с таким именем, —
# и делал это молча, отвечая «мёртвых имён не нашлось».
TOOL_DEAD_SKIP = ('py_modules/', 'scratchpad/', 'node_modules/')

# Слово целиком: `photo` не должно стрелять внутри `photo_batch`.
TOOL_DEAD_WORD = re.compile(r'\w+')


def tool_dead(root='.', private_only=False, search=None) -> list[dict]:
    """Определённые имена, которых никто не читает: кандидаты на удаление.

    Args:
        root: где **судить** — файл или подкаталог.
        private_only: судить только приватные имена (`_name`). Нужно для общего
            слоя: его публичные функции зовут из других репозиториев.
        search: где **искать** использование; пусто — весь репозиторий, в котором
            лежит `root`.

    Returns:
        list[dict]: записи `{name, kind, file, line, verdict, seen}` — имя, что это
        (`func`, `class`, `const`), где определено, вердикт из трёх (`TOOL_DEAD_GONE`
        и два «только…») и слои, где имя всё же встретилось. Пусто — мёртвого нет.

    ⚠⚠ Два круга разные, и это главное в устройстве: судим подкаталог, а ищем по
    **всему репозиторию**. Цена измерена: с поиском внутри `src` из 206 находок
    ложными оказались 48 — зовущие лежали в `tests/`, в `main_uvicorn.py` и в доках,
    то есть ровно там, куда обход не заходил. Сведи оба круга в один — и инструмент
    начнёт уверенно предлагать удалять рабочий код.

    ⚠⚠ Судятся имена **уровня модуля**: методы класса сюда не попадают. Метод,
    реализующий протокол, зовут через базу — по имени его не найти, и отчёт был бы
    сплошь из ложных находок.

    ⚠ Порядок — по файлу и строке: отчёт читают, открыв файл рядом.
    """
    base = Path(root).resolve()
    whole = Path(search).resolve() if search else _repo_of(base)

    judged = [one for one in file_walk(base, ('.py',)) if not _skipped(one, base)]
    files = [one for one in file_walk(whole, TOOL_IMPACT_SUFFIXES)
             if not _skipped(one, whole)]

    defined = {}
    for one in judged:
        for got in _defs(one):
            if private_only and not got['name'].startswith('_'):
                continue
            defined.setdefault(got['name'], []).append(got)

    seen = _seen(files, set(defined))
    out = []

    for name, places in defined.items():
        # ⚠ Имя, определённое дважды (переопределение, ветка `try/except ImportError`),
        # не судится: какое из определений мёртвое — вопрос не к счётчику.
        if len(places) != 1:
            continue

        place = places[0]
        where = {layer for layer, count in seen.get(name, {}).items() if count > 0}
        own = seen.get(name, {}).get(_layer(place['file']), 0)

        # Своё определение — тоже вхождение; вычитаем его из слоя, где оно стоит.
        if own <= 1:
            where.discard(_layer(place['file']))

        verdict = _verdict(where)
        if verdict:
            out.append({**place, 'verdict': verdict, 'seen': sorted(where)})

    return sorted(out, key=lambda one: (one['file'], one['line']))


def tool_dead_format(found, root='.') -> str:
    """Отчёт словами. Пусто — мёртвых имён не нашлось."""
    if not found:
        return 'Мёртвых имён не нашлось.'

    base = Path(root).resolve()
    out = [f'Кандидатов: {len(found)}.']
    file_now = ''

    for one in found:
        short = _short(base, one['file'])
        if short != file_now:
            file_now = short
            out.append(f'\n── {short}')

        tail = f" (встречается: {', '.join(one['seen'])})" if one['seen'] else ''
        out.append(f"   {one['line']:>5}  {one['verdict']:13} "
                   f"{one['kind']:5} {one['name']}{tail}")

    out.append('\n⚠ Это кандидаты: имя может приезжать из базы, из workflow соседа '
               'или из шаблона вне дерева. Решает человек.')

    return '\n'.join(out)


def main() -> int:
    """CLI: код возврата — число имён с вердиктом `мёртвое`."""
    import argparse

    ap = argparse.ArgumentParser(
        description='Имена, которые определены, но никто их не читает.')
    ap.add_argument('root', nargs='?', default='.', help='корень дерева')
    ap.add_argument('--private', action='store_true',
                    help='только приватные имена — режим общего слоя')
    ap.add_argument('--only', default='',
                    help=f'один вердикт: {TOOL_DEAD_GONE}, {TOOL_DEAD_TESTS_ONLY}, '
                         f'{TOOL_DEAD_DOC_ONLY}')
    ns = ap.parse_args()

    found = tool_dead(ns.root, private_only=ns.private)
    if ns.only:
        found = [one for one in found if one['verdict'] == ns.only]

    print(tool_dead_format(found, ns.root))

    return sum(1 for one in found if one['verdict'] == TOOL_DEAD_GONE)


def _defs(path: Path) -> list[dict]:
    """Имена уровня модуля, определённые в файле: функции, классы, константы.

    ⚠ Файл с синтаксической ошибкой пропускается молча: инструмент вспомогательный
    и падать из-за чужой недописанной правки не должен.
    """
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    except (SyntaxError, ValueError):
        return []

    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _alive(node):
                continue
            kind = 'class' if isinstance(node, ast.ClassDef) else 'func'
            out.append({'name': node.name, 'kind': kind,
                        'file': str(path), 'line': node.lineno})
            continue

        # Константы: только `UPPER_CASE` уровня модуля. ⚠ Прочие присваивания не
        # берём — модульное состояние (`pool`, `logger`) читают не по имени.
        for name in _assigned(node):
            if name.isupper():
                out.append({'name': name, 'kind': 'const',
                            'file': str(path), 'line': node.lineno})

    return out


def _alive(node) -> bool:
    """Зовёт ли это имя каркас, а не код: маршрут, тест, фикстура, `main`."""
    if node.name in TOOL_DEAD_ALIVE:
        return True
    if node.name.startswith(TOOL_DEAD_ALIVE_STARTS):
        return True

    for one in getattr(node, 'decorator_list', ()):
        try:
            text = ast.unparse(one)
        except (AttributeError, ValueError):      # ⚠ на всякий случай: см. ⚠⚠ выше
            text = ast.dump(one)

        if any(mark in text for mark in TOOL_DEAD_ALIVE_MARKS):
            return True

    return False


def _assigned(node) -> list[str]:
    """Имена, которым присваивают на уровне модуля."""
    if isinstance(node, ast.AnnAssign):
        return [node.target.id] if isinstance(node.target, ast.Name) else []

    if not isinstance(node, ast.Assign):
        return []

    return [one.id for one in node.targets if isinstance(one, ast.Name)]


def _seen(files, names) -> dict:
    """Сколько раз каждое имя встречается, по слоям.

    ⚠⚠ Один проход по файлам, а не поиск на каждое имя: имён сотни и файлов сотни,
    и поиск по каждой паре был бы в сто раз дороже разбора. Слова файла берутся
    множеством, дальше — пересечение с искомыми.
    """
    out = {}

    for one in files:
        text = _stripped(one)
        if not text:
            continue

        layer = _layer(str(one))
        for word in TOOL_DEAD_WORD.findall(text):
            if word in names:
                out.setdefault(word, {})
                out[word][layer] = out[word].get(layer, 0) + 1

    return out


def _stripped(path: Path) -> str:
    """Текст файла без комментариев и докстрингов — см. ⚠⚠ в докстринге модуля.

    ⚠⚠ Проза (`.md`, `.txt`) не вычёркивается вовсе, и это не поблажка. В markdown
    `#` — **заголовок**, то есть содержание; вычёркиватель принимает его за
    комментарий и сносит строку целиком. Имя, стоящее в заголовке темы, пропадало
    из виду, и вердикт «только док» не мог случиться в принципе.

    ⚠ Упоминание в прозе и так не считается использованием — для этого и есть
    отдельный вердикт. Вычёркивать здесь нечего.
    """
    try:
        if path.suffix == '.py':
            return pytest_guard_code(str(path))

        text = path.read_text(encoding='utf-8', errors='replace')
        if path.suffix in ('.md', '.txt'):
            return text

        return pytest_guard_text(text)
    except (OSError, ValueError, SyntaxError):
        return ''


def _layer(where: str) -> str:
    """Слой файла: тесты, док или код. ⚠ Те же слои, что у `tool_impact`."""
    said = _slash(where)

    if '/tests/' in said or said.split('/')[-1].startswith('test_'):
        return TOOL_DEAD_TESTS
    if said.endswith(('.md', '.txt')):
        return TOOL_DEAD_DOC

    return 'код'


def _verdict(where) -> str:
    """Вердикт по слоям, где имя встретилось. Пусто — имя живое."""
    if not where:
        return TOOL_DEAD_GONE
    if where == {TOOL_DEAD_TESTS}:
        return TOOL_DEAD_TESTS_ONLY
    if where == {TOOL_DEAD_DOC}:
        return TOOL_DEAD_DOC_ONLY

    return ''


def _skipped(path, base: Path) -> bool:
    """Лежит ли файл в ветке, которую не берём. ⚠ Путь — от корня обхода."""
    said = _slash(_short(base, path))

    return any(bad.strip('/') in said.split('/') for bad in TOOL_DEAD_SKIP)


def _repo_of(path: Path) -> Path:
    """Корень репозитория, в котором лежит путь: по `.git` вверх по дереву.

    ⚠ `.git` проверяется на существование, а не на «каталог»: у подмодуля это
    **файл** со ссылкой. Иначе обход ушёл бы выше — в родительский проект, — и
    библиотека судилась бы вместе с ним.

    ⚠ Не нашлось — остаётся сам путь: инструмент должен работать и вне репозитория.
    """
    for one in (path, *path.parents):
        if (one / '.git').exists():
            return one

    return path


def _slash(where) -> str:
    """Путь с прямыми косыми — чтобы сверка путей не зависела от системы."""
    return str(where).replace('\\', '/')


def _short(base: Path, path) -> str:
    """Путь от корня обхода: в отчёте абсолютные только мешают."""
    try:
        return str(Path(path).resolve().relative_to(base))
    except ValueError:
        return str(path)


if __name__ == '__main__':
    sys.exit(main())

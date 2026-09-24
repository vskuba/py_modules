"""
Сторож приёма, который не ловит собственные объяснения.

Сторож приёма — тест, следящий не за поведением, а за устройством: «правило
живёт в одном месте», «эта регулярка не вернулась», «переход стоит до вызова».
Пишут его поиском подстроки, и на этом он ломается: **имя убранного правила
называют в комментарии рядом с заменой** — иначе следующий читатель не поймёт,
отчего так, — и поиск находит объяснение вместо возврата.

Замерено: за одну сессию трижды. Тест искал `_telegram_notify_login`,
`initiative`, `<think>.*?</think>` — и каждый раз краснел на комментарии,
который сам же и объяснял правку.

## Чем отвечает

| | вопрос, на который отвечает |
|---|---|
| `pytest_guard_code()` | «есть ли это в коде вообще» — без комментариев и docstring |
| `pytest_guard_calls()` | «зовут ли это» — разбором вызовов |
| `pytest_guard_kwargs()` | «как именно зовут» — какими доводами |
| `pytest_guard_text()` | то же для не-Python: SQL, разметка, стили |

⚠ **Выбирать надо по вопросу, а не по удобству.** `pytest_guard_code` снимает
объяснения, но объявленную рядом функцию видит — она и есть код. Спрашивать «не
подписан ли обработчик» надо у `pytest_guard_calls`: имя, объявленное и не
позванное, для него не существует. Замер на живом случае:

    поиском подстрокой  : True   ← ложная тревога, имя в комментарии
    pytest_guard_code   : True   ← честно: функция объявлена рядом
    pytest_guard_calls  : False  ← верный ответ: не подписан

## ⚠ Строковые литералы остаются

Соблазн выбросить и строки велик, но искомое часто **и есть строка**: регулярка,
имя переменной в промпте, кусок SQL. Убери их — сторож перестанет видеть даже
вернувшееся правило. Поэтому уходят комментарии и docstring, а литералы живут.

## ⚠ Это не про поведение

Сторож приёма дополняет проверку работы, а не заменяет её. Он отвечает «правило
на месте», и только; что оно делает верно — знает обычный тест.
"""
import argparse
import ast
import inspect
import re
import sys
import textwrap
from pathlib import Path

# Комментарии, которые снимаются из не-Python текста: SQL, JS, разметка.
# ⚠ `#` тоже комментарий MySQL, но в CSS это цвет, а в разметке — якорь;
# поэтому он снимается только там, где стоит с начала строки.
PYTEST_GUARD_COMMENT_RE = (
    (re.compile(r'/\*.*?\*/', re.S), ' '),
    (re.compile(r'<!--.*?-->', re.S), ' '),
    (re.compile(r'^\s*(--|#).*$', re.M), ''),
    (re.compile(r'(?<![:\w])//.*$', re.M), ''),
)


def pytest_guard_code(target) -> str:
    """
    Сторож приёма: код без комментариев и docstring, искать подстрокой.

    Args:
        target: функция, класс, модуль или путь к файлу.

    Returns:
        Исходник, из которого убраны комментарии и docstring. Литералы целы.

    Raises:
        ValueError: исходник не читается или не разбирается.

    ⚠ Именно docstring, а не «первая строка»: у вложенных функций и классов они
    свои, и объяснение чаще всего лежит как раз там.
    """
    tree = ast.parse(_source_of(target))

    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if not isinstance(body, list):
            continue
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)

    return ast.unparse(tree)


def pytest_guard_calls(target) -> set:
    """
    Какие функции вправду зовут внутри.

    Args:
        target: функция, класс, модуль или путь к файлу.

    Returns:
        Имена вызываемых: `foo`, `obj.method` — как написаны в коде.

    ⚠ Разбором вызовов, а не поиском имени: упомянуть функцию в комментарии,
    строке или docstring — не то же, что позвать её. Именно на этом краснели
    сторожа подписок на события.
    """
    out = set()
    for node in ast.walk(ast.parse(_source_of(target))):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name:
            out.add(name)

    return out


def pytest_guard_kwargs(target, call: str = '') -> dict:
    """
    С какими именованными доводами зовут функции.

    Args:
        target: где смотреть.
        call: только этот вызов; пусто — все.

    Returns:
        `{имя вызова: {довод: текст значения}}`.

    ⚠ Нужно там, где важно **как** позвали, а не что: `mail=True` против
    `initiative=…` — разные ветки промпта, и различить их можно только здесь.
    """
    out = {}
    for node in ast.walk(ast.parse(_source_of(target))):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if not name or (call and name != call):
            continue
        got = {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
        if got:
            out.setdefault(name, {}).update(got)

    return out


def pytest_guard_text(text: str) -> str:
    """
    Текст без комментариев — для не-Python: SQL, разметка, стили.

    Args:
        text: содержимое файла.

    Returns:
        То же, но без `--`, `#` в начале строки, `/* */`, `<!-- -->` и `//`.

    ⚠ Разбора здесь нет, только вычёркивание: полноценно разобрать разметку
    дороже, чем стоит сторож. Поэтому правило обратное — **в комментариях рядом
    с такой проверкой искомое пишут словами**, а не как оно выглядит в коде.
    """
    out = str(text)
    for pattern, repl in PYTEST_GUARD_COMMENT_RE:
        out = pattern.sub(repl, out)

    return out


def _source_of(target) -> str:
    """Исходник цели: путь, модуль, класс или функция."""
    if isinstance(target, (str, Path)):
        path = Path(target)
        if path.exists():
            return path.read_text(encoding='utf-8')
        raise ValueError(f'файла нет: {target}')

    try:
        return textwrap.dedent(inspect.getsource(target))
    except (OSError, TypeError) as err:
        raise ValueError(f'исходник не читается: {err}') from err


def _call_name(node) -> str:
    """Имя вызываемого: `foo`, `mod.foo`, `obj.method`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)

        return f'{parent}.{node.attr}' if parent else node.attr

    return ''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Код без объяснений: для сторожей приёма.')
    parser.add_argument('command', choices=['code', 'calls', 'kwargs', 'text'])
    parser.add_argument('path', help='файл')
    parser.add_argument('--call', default='', help='для kwargs: только этот вызов')
    args = parser.parse_args()

    try:
        if args.command == 'code':
            print(pytest_guard_code(args.path))
        elif args.command == 'calls':
            print('\n'.join(sorted(pytest_guard_calls(args.path))))
        elif args.command == 'kwargs':
            for name, kw in sorted(pytest_guard_kwargs(args.path, args.call).items()):
                print(f'{name}: ' + ', '.join(f'{k}={v}' for k, v in sorted(kw.items())))
        else:
            print(pytest_guard_text(Path(args.path).read_text(encoding='utf-8')))
    except (ValueError, SyntaxError) as err:
        raise SystemExit(f'ошибка: {err}')

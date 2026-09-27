"""Одинаковые тела, названные разными словами: кандидаты стать одним паттерном.

Правило — `docs/code_rules.md` §4.2: три места делают одно и то же — это не три
функции, а один паттерн с точкой различия. Порог названо числом (правило трёх) и
не зависит от того, «кажется ли похоже» автору.

## Чем инструмент, а не heredoc из правил

Автопроверка в правилах сравнивает `ast.dump` тел — и ловит лишь **буквальную**
копию: «тела с заменёнными именами переменных деревья различают, и „похожее с
двумя другими словами“ проверка пропустит» — сказано там же, с досадой. Здесь
тела нормализуются: аргументы и локальные имена получают места по порядку
появления, чужие имена (константы настроек, ключи) — тоже. После этого семь
обёрток круга, различавших ключами настройки и словом в журнал, совпадают
деревом, и правило трёх становится вызовом, а не цитатой.

## ⚠ Это подсказка, а не запрет

Как и группы в §1.1: вывод не обязан быть пустым, осознанный дубль живёт,
если объяснён в докстринге. Инструмент показывает **что** сошлось деревом —
что это один предмет или два разных (ложное обобщение дороже дубля) решает
человек, как §4.2 и велит.
"""
import argparse
import ast
import collections
import sys
from pathlib import Path

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Каталоги, которые не смотрим никогда: чужой код и следы сборки.
FUNCTION_TWIN_SKIP = ('__pycache__', '.venv', 'venv', 'node_modules', '.git',
                      'migrations')


def function_twin(root: str, min_nodes: int = 5) -> list:
    """Совпавшие тела определений — группы кандидатов в один паттерн.

    Args:
        root: файл или дерево `.py`.
        min_nodes: узлов в теле без докстринга, меньше — шум, а не копия
            (порог унаследован из автопроверки §4.2).

    Returns:
        Список групп; группа — список `{'file', 'line', 'name'}`, определения
        с совпавшим деревом тела после нормализации имён. Тела сравниваются
        без докстрингов: докстринг объясняет, но не различает.

    Raises:
        RuntimeError: путь не существует.
    """
    path = Path(root)

    if not path.exists():
        raise RuntimeError(f'нет такого пути: {root}')

    files = [path] if path.is_file() else sorted(
        one for one in path.rglob('*.py')
        if not any(part in FUNCTION_TWIN_SKIP for part in one.parts))

    seen = collections.defaultdict(list)

    for one in files:
        try:
            tree = ast.parse(one.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body[1:] if ast.get_docstring(node) else list(node.body)
            if len(body) < min_nodes:
                continue
            seen[_fingerprint(body)].append(
                {'file': str(one), 'line': node.lineno, 'name': node.name})

    return [sorted(group, key=lambda d: (d['file'], d['line']))
            for group in seen.values() if len(group) > 1]


def function_twin_format(groups: list) -> str:
    """Отчёт словами: каждая группа — с одной строки на близнеца."""
    if not groups:
        return 'тел, совпавших деревом, нет'

    lines = []
    for num, group in enumerate(groups, 1):
        lines.append(f'\n── кандидат {num}: одно дело в {len(group)} местах')
        lines += [f"   {d['name']} (стр. {d['line']}) — {d['file']}" for d in group]

    lines.append(f'\nгрупп: {len(groups)}')
    return '\n'.join(lines).lstrip('\n')


def _fingerprint(body: list) -> str:
    """Дерево тела после нормализации: всякое имя получает место по порядку.

    Аргументы, локальные и чужие для тела имена (константы настроек, ключи)
    становятся `_0, _1…` по первому появлению. После этого два тела,
    различавшиеся только именами, дают одно дерево — ровно то, чего не хватало
    текстовому рецепту из правил: «тела с заменёнными именами переменных
    деревья различают», и «похожее с двумя другими словами» проходило мимо.
    """
    tree = ast.Module(body=body, type_ignores=[])
    tree = _Norm().visit(tree)
    return ast.dump(ast.Module(body=tree.body, type_ignores=[]),
                    include_attributes=False)


class _Norm(ast.NodeTransformer):
    """Имя → его позиция первого появления. Порядок появления один — дерево одно."""

    def __init__(self):
        self.names, self.consts = {}, {}

    @staticmethod
    def _slot(seen: dict, key) -> str:
        """Место имени в порядке первого появления: `_0`, `_1`…

        ⚠ Метода не было вовсе — три `visit_*` ниже звали его с первой версии, и
        `function_twin` падал `AttributeError` на любом непустом теле. То есть
        проверка «совпавшие тела», обещанная правилами (§4.2, «Автопроверка»), не
        работала ни разу с момента написания.

        ⚠⚠ Своя таблица на каждый вид имени (`names`, `consts`) — не мелочь:
        общая свела бы переменную и строковый литерал в одно место, и два тела,
        различающиеся **только** ключом настройки, стали бы близнецами.
        """
        if key not in seen:
            seen[key] = f'_{len(seen)}'

        return seen[key]

    def visit_Name(self, node):
        node = self.generic_visit(node)
        node.id = self._slot(self.names, node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._slot(self.names, node.arg)
        return node

    def visit_Constant(self, node):
        # ключ настройки и слово в журнал — строки: в близнецах круга различие
        # живёт именно там, и оно не должно прятаться в литерале.
        node.value = self._slot(self.consts, repr(node.value))
        return node


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Тела, совпавшие деревом: кандидаты стать одним паттерном (§4.2).')
    parser.add_argument('root', help='файл или дерево')
    parser.add_argument('--min', type=int, default=5, help='узлов в теле, меньше — шум')
    args = parser.parse_args()

    try:
        print(function_twin_format(function_twin(args.root, args.min)))
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

    raise SystemExit(0)

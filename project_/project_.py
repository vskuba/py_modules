"""
Где корень проекта, чем в нём запускают Python и с каким окружением.

Модуль отвечает на три вопроса, которые каждый разовый скрипт выясняет заново — и
ошибается на них одинаково.

**Где корень.** Обход вверх от рабочего каталога границы репозитория не знает и
находит чужой `.env` в домашнем каталоге — та же беда, что увела `config` на счёт
от себя (см. его шапку). Корень берётся от места самого модуля:
`<корень>/py_modules/project_/`, и это верно, откуда бы процесс ни запустили.

**Чем запускать.** Системный `python3` зависимостей проекта не видит — они в
`.venv`. Отсюда `ModuleNotFoundError` на первой же строке импорта, хотя пакет
установлен.

**С каким `PYTHONPATH`.** Корень **и** `py_modules`, оба сразу: приложение
получает их из compose, тесты — из `pyproject.toml`, а разовый скрипт не получает
ниоткуда, и `from mysql_.mysql_ import …` падает. Порядок тот же, что в бою, —
модули грузятся под теми же именами, а не вторыми копиями (`py_modules.md`, §2).

**Про выкладки.** Роль работает в `<корень>/.claude/worktrees/<ветка>` — это
отдельный рабочий каталог того же репозитория. Для Python он самостоятельный
корень: свой `py_modules`, симлинки на `.env` и `.venv`. А всё, что адресуется
*репозиторием*, а не каталогом, должно смотреть на основную выкладку —
`project_main_root`.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

# Потолок на один вопрос к git. Секунды: `rev-parse` отвечает мгновенно, а
# git в каталоге на сетевой шаре или с битым индексом висит — и вешает точку
# входа, которая всего лишь спросила, где корень.
PROJECT_GIT_TIMEOUT = 10.0


def project_root() -> Path:
    """
    Корень проекта, в который подключён py_modules.

    Считается от места модуля (`<корень>/py_modules/project_/project_.py`), а не
    обходом вверх от рабочего каталога: тот уводит в чужой репозиторий.

    Returns:
        Абсолютный путь к корню. Для выкладки — корень самой выкладки.
    """
    return Path(__file__).resolve().parents[2]


def project_main_root() -> Path:
    """
    Корень основной выкладки — один и тот же для проекта и всех его worktree.

    Нужен всему, что помнит проект по каталогу, а не по рабочей копии. Живой
    пример: docker compose запоминает имя проекта от каталога, где его подняли,
    поэтому из выкладки `docker compose port <сервис> <порт>` отвечает
    «service is not running», хотя контейнер работает.

    Returns:
        Корень основной выкладки; вне git — то же, что `project_root()`.
    """
    root = project_root()
    # `--git-common-dir` в обычной копии отдаёт относительный `.git`, в выкладке —
    # абсолютный путь к `.git` основной. И то и другое сводится к «родитель `.git`».
    common = project_git(root, '--git-common-dir')
    if not common:
        return root

    path = Path(common)
    if not path.is_absolute():
        path = (root / path).resolve()
    return path.parent if path.name == '.git' else root


def project_python() -> Path:
    """
    Интерпретатор проекта: из виртуального окружения, если оно есть.

    Порядок поиска — активное окружение (`VIRTUAL_ENV`), `.venv` и `venv` в корне,
    `.venv` основной выкладки, и только затем текущий интерпретатор. Последний
    шаг — честный запасной путь, а не рабочий: зависимостей проекта в нём обычно
    нет, и импорт упадёт.

    Returns:
        Путь к исполняемому файлу интерпретатора.
    """
    venv = os.environ.get('VIRTUAL_ENV')
    if venv and _executable(Path(venv) / 'bin' / 'python'):
        return Path(venv) / 'bin' / 'python'

    root = project_root()
    for candidate in (root / '.venv' / 'bin' / 'python', root / 'venv' / 'bin' / 'python'):
        if _executable(candidate):
            return candidate

    # Основную выкладку спрашиваем последней: она стоит запуска git.
    candidate = project_main_root() / '.venv' / 'bin' / 'python'
    return candidate if _executable(candidate) else Path(sys.executable)


def project_env(extra: dict | None = None, strip: tuple = ()) -> dict:
    """
    Окружение дочернего процесса: `PYTHONPATH` с корнем проекта и с `py_modules`.

    Унаследованный `PYTHONPATH` не затирается, а дописывается в хвост — свои пути
    должны побеждать.

    Args:
        extra: переменные поверх готового окружения; значения приводятся к строке.
        strip: имена унаследованных переменных, которые дочернему процессу не
            достаются (с хвостовым `*` — префикс целиком). Без этого чужой шум
            побеждает проект: `config` читает `.env` без `override`, и то, что
            уже пришло в `os.environ`, из `.env` уже не вытеснить.

    Returns:
        Словарь, готовый для `subprocess(env=…)`.
    """
    root = project_root()
    parts = [str(root), str(root / 'py_modules')]
    inherited = os.environ.get('PYTHONPATH')
    if inherited:
        parts.append(inherited)

    env = dict(os.environ)
    for pat in strip:
        if pat.endswith('*'):
            for k in [k for k in env if k.startswith(pat[:-1])]:
                del env[k]
        else:
            env.pop(pat, None)
    env['PYTHONPATH'] = os.pathsep.join(parts)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def project_git(cwd, *args: str) -> str:
    """Спросить git одну строку в этом каталоге; не ответил — пустая строка.

    Args:
        cwd: каталог, из которого спрашиваем (корень репозитория или чекаут
            подмодуля — git сам разберётся).
        *args: аргументы после `git` (`'rev-parse', 'HEAD'`).

    Returns:
        stdout без хвостовых пробелов; пусто — git не установлен, каталог не
        репозиторий или команда вернула ненулевой код.

    ⚠ Отказ неотличим от пустого ответа — так задумано: вызывающему здесь
    нужен факт («вот sha» / «sha нет»), а не разбор причины. Нужна причина —
    зовите `subprocess` сами.

    ⚠ Потолок 10 с: `git` в каталоге на сетевой шаре или с битым индексом
    висит, а корень проекта спрашивают из каждой точки входа.
    """
    try:
        done = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=PROJECT_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout.strip() if done.returncode == 0 else ''


# ── детали реализации ──

def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Корень проекта, его интерпретатор и окружение.')
    parser.add_argument('command', choices=['root', 'main-root', 'python', 'env'],
                        help='root — корень выкладки; main-root — корень репозитория; '
                             'python — интерпретатор; env — переменные для дочернего процесса')
    parser.add_argument('--only', default='',
                        help='env: только ключи с этими префиксами через запятую '
                             '(значения из .env, прочитанного конфигом)')
    args = parser.parse_args()

    if args.command == 'root':
        print(project_root())
    elif args.command == 'main-root':
        print(project_main_root())
    elif args.command == 'python':
        print(project_python())
    elif args.only:
        from config.config import config_get   # импорт читает .env в os.environ
        for key in sorted(os.environ):
            if any(key.startswith(p.strip()) for p in args.only.split(',')):
                print(f'{key}={config_get(key)}')
    else:
        print(f"PYTHONPATH={project_env()['PYTHONPATH']}")

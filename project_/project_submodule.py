"""
Распря указателя `py_modules`: что принесёт bump и как слить линии без потерь.

`py_modules` живёт двумя чекаутами одного репозитория: самостоятельный (где
правят общий слой) и submodule `<корень>/py_modules` (где его читает панель).
Линии расходятся молча: gitlink указывает на старый кончик, пока человек не
вспомнил про указатель, а правка уехала в другой чекаут. Симптом — панель
крутится без свежей правки и молчит; проверить без инструмента нельзя, потому
что «чего не хватает» у detached HEAD не спрашивают.

Здесь два вопроса, на которые модуль отвечает словами, и один танец делает сам:

* **что сейчас**: gitlink (что зафиксировано проектом), кончик чекаута, их родство
  (кто кому предок — `merge-base --is-ancestor`), и чем именно отличается файл;
* **что принесёт bump**: список файлов между gitlink и целью; если кончик чекаута
  не предок цели, bump без слияния **украдёт** строку чекаута — модуль это
  говорит до, а не после;
* **слить и встать**: `project_submodule_bump` fetch'ит цель из второго чекаута
  (`--from`), вливает неслитое линией через временную ветку, как это делают
  руками, и оставляет кончик на объединении.

Коммит указателя остаётся человеком: модуль называет sha, а `git add py_modules`
и сообщение — дело того, кто коммитит.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def project_submodule_state() -> dict:
    """Что сейчас: gitlink, чекаут, родство линий, чем отличаются.

    Вне submodule-контекста (самостоятельный чекаут общего слоя) gitlink пуст —
    это не ошибка, а факт: здесь правят линию сразу, а не указатель.

    Returns:
        {'gitlink': sha из дерева проекта ('' — submodule-контекста нет),
         'checkout': HEAD чекаута, 'synced': равны, 'ancestor': gitlink предок
         чекаута (без слияния bump ничего не украдёт), 'files':
         `diff --stat gitlink..checkout` — что приедет в панель при bump}
    """
    root, sub = _repo()
    parts = _git(root, 'ls-tree', 'HEAD', sub.name).split() if root != sub else []
    # «160000 commit <sha>\t<путь>»: полем больше — и это не gitlink
    gitlink = parts[2] if len(parts) == 4 and parts[1] == 'commit' else ''
    checkout = _git(sub, 'rev-parse', 'HEAD')
    ancestor = bool(gitlink and checkout) and _run_ok(
        sub, 'merge-base', '--is-ancestor', gitlink, checkout)
    files = _git(sub, 'diff', '--stat', gitlink, checkout).splitlines() \
        if gitlink and checkout else []
    return {'gitlink': gitlink, 'checkout': checkout,
            'synced': bool(gitlink) and gitlink == checkout, 'ancestor': ancestor,
            'files': files}


def project_submodule_bump(line: str = 'master', from_repo: str = '') -> dict:
    """Встать на кончик линии, влив в него неслитое; вернуть, на что встали.

    Танец целиком: `--from` (второй чекаут) — fetch цели оттуда; если HEAD
    чекаута не предок цели, HEAD вливается в `<line>` слиянием и итог —
    объединение обеих линий. Указатель проекта после этого правят руками
    (`git add py_modules`), модуль лишь называет sha.

    Args:
        line: цель — имя ветки или sha; с `from_repo` — та же ветка **в том**
            чекауте (её же имя fetch заберёт оттуда).
        from_repo: путь второго чекаута, откуда взять линию (самостоятельный
            чекаут для проекта и наоборот).

    Returns:
        {'sha': итоговый кончик, 'merged': было ли слияние,
         'files': diff --stat старый gitlink..новый sha, пусто — линия полная}

    Raises:
        ValueError: цель не читается (нет такой ветки/sha нигде из названных).
    """
    root, sub = _repo()
    head = _git(sub, 'rev-parse', 'HEAD')
    target = line
    if from_repo:
        # та же линия в двух чекаутах живёт по-разному: свежее там, где правят
        here = _git(sub, 'rev-parse', '--verify', line)
        there = _git(Path(from_repo).resolve(), 'rev-parse', '--verify', line)
        if there and (not here or here != there):
            _run(sub, 'fetch', '-q', str(Path(from_repo).resolve()), line)
            target = 'FETCH_HEAD'
    if not _git(sub, 'rev-parse', '--verify', target):
        raise ValueError(f'цель {line!r} не читается в {sub.name}: ни ветка, ни sha')
    sha = _git(sub, 'rev-parse', target)

    merged = bool(sha) and bool(head) and not _run_ok(
        sub, 'merge-base', '--is-ancestor', head, sha)
    if merged:
        # линия чекаута не предок цели — вливаем её в цель временной веткой,
        # как это делают руками: цель победит, но не потеряет чужое
        _run(sub, 'branch', '-q', '_psm-tmp', head)
        _run(sub, 'checkout', '-q', sha)
        _run(sub, 'merge', '-q', '--no-edit', '_psm-tmp')
        _run(sub, 'branch', '-q', '-D', '_psm-tmp')
    else:
        _run(sub, 'checkout', '-q', sha)

    parts = _git(root, 'ls-tree', 'HEAD', sub.name).split() if root != sub else []
    gitlink = parts[2] if len(parts) == 4 and parts[1] == 'commit' else ''
    final = _git(sub, 'rev-parse', 'HEAD')
    return {'sha': final, 'merged': merged,
            'files': _git(sub, 'diff', '--stat', gitlink, final).splitlines()
            if gitlink else []}


# ── детали реализации ──

def _repo() -> tuple[Path, Path]:
    """(корень проекта, чекаут py_modules).

    Чекаут — каталог этого модуля с точностью до папки пакета: он сам знает,
    куда его подключили; `--show-superproject-working-tree` отвечает, есть ли
    над ним проект с gitlink (в standalone-чекауте его нет — возвращаем сам
    чекаут, gitlink тогда пуст).
    """
    here = Path(__file__).resolve().parents[1]
    super_ = _git(here, 'rev-parse', '--show-superproject-working-tree')
    return (Path(super_) if super_ else here), here


def _git(cwd: Path, *args: str) -> str:
    """Одна строка от git; ошибка — пусто (git сам скажет, чего не знает)."""
    try:
        done = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout.strip() if done.returncode == 0 else ''


def _run_ok(cwd: Path, *args: str) -> bool:
    try:
        return subprocess.run(['git', *args], cwd=str(cwd), capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(cwd: Path, *args: str) -> None:
    """Команда git, обязанная удасться: чужой текст наружу как есть."""
    subprocess.run(['git', *args], cwd=str(cwd), check=True, capture_output=True,
                   text=True, timeout=20)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Указатель py_modules: state — что и как расходится, '
                    'bump — встать на линию, влив неслитое.')
    ap.add_argument('command', choices=['state', 'bump'])
    ap.add_argument('--line', default='master', help='цель: ветка или sha')
    ap.add_argument('--from', dest='from_repo', default='',
                    help='путь второго чекаута, откуда взять линию')
    ns = ap.parse_args()
    try:
        out = (project_submodule_state() if ns.command == 'state'
               else project_submodule_bump(ns.line, ns.from_repo))
        print(json.dumps(out, ensure_ascii=False, indent=1))
    except (ValueError, subprocess.CalledProcessError) as err:
        raise SystemExit(f'ошибка: {err}')

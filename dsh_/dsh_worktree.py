"""Своя копия репозитория (`git worktree`) для субагента, который пишет код.

Субагент DeepSeek, правящий файлы прямо в рабочей копии оркестратора, затирал бы его
незакоммиченные правки и мешал бы соседним субагентам. Поэтому пишущему субагенту —
своя копия на своей ветке: он правит там, оркестратор смотрит diff (`dsh_worktree_diff`)
и сам решает, что сливать. Копии лежат в `<repo>/.claude/worktrees/` — там же, где
копии ролей конвейера, — и не попадают в `git status` основной копии.

```python
from dsh_.dsh_worktree import dsh_worktree_new, dsh_worktree_diff, dsh_worktree_drop

tree = dsh_worktree_new('~/PycharmProjects/BlogAi', 'фикс тестов')
# ... субагенту — карточку в tree['path'] (dsh-board new --worktree) ...
print(dsh_worktree_diff(tree['path']))
dsh_worktree_drop(tree['path'])          # ветка dsh/… остаётся, пока её не сольют
```
"""
import os
import re
import secrets
import subprocess
import time

# Где лежат копии внутри репозитория и как зовутся их ветки.
DSH_WORKTREE_DIR = os.path.join('.claude', 'worktrees')
DSH_WORKTREE_BRANCH_PREFIX = 'dsh/'
# Потолок одной команды git, с.
DSH_WORKTREE_GIT_TIMEOUT = 120


class DshWorktreeError(RuntimeError):
    """git отказал: не репозиторий, ветка занята, копия не снимается."""


def dsh_worktree_new(repo: str, name: str) -> dict:
    """Завести копию репозитория на новой ветке от текущего коммита.

    Args:
        repo: каталог внутри репозитория (корень найдётся сам).
        name: человеческое имя задачи — из него слаг ветки и каталога.

    Returns:
        `{"path", "branch", "root"}` — каталог копии, её ветка, корень репозитория.
    """
    root = _git(os.path.expanduser(repo), 'rev-parse', '--show-toplevel').strip()
    slug = _slug(name)
    branch = f'{DSH_WORKTREE_BRANCH_PREFIX}{slug}'
    path = os.path.join(root, DSH_WORKTREE_DIR, f'dsh-{slug}')
    _git(root, 'worktree', 'add', '-b', branch, path, 'HEAD')
    return {'path': path, 'branch': branch, 'root': root}


def dsh_worktree_diff(path: str, stat_only: bool = False) -> str:
    """Что поменяно в копии относительно её начала: коммиты субагента и незакоммиченное.

    Новые, ещё не добавленные файлы тоже попадают в ответ: их перечисляем отдельно —
    субагент не обязан их коммитить.
    """
    base = _git(path, 'merge-base', 'HEAD', _start_ref(path)).strip()
    args = ['diff', '--stat'] if stat_only else ['diff', '--stat', '--patch']
    diff = _git(path, *args, base)
    untracked = _git(path, 'ls-files', '--others', '--exclude-standard').strip()
    if untracked:
        diff += '\n# новые файлы, не добавленные в git:\n' + untracked + '\n'
    return diff or 'изменений нет'


def dsh_worktree_dirty(path: str) -> bool:
    """Есть ли в копии незакоммиченные правки (новые файлы — тоже); нет копии — False."""
    if not os.path.isdir(path):
        return False
    return bool(_git(path, 'status', '--porcelain', check=False).strip())


def dsh_worktree_land(path: str, message: str) -> dict:
    """Принять работу субагента: закоммитить её в его ветке и слить в основную копию.

    Незакоммиченное в копии коммитится одним коммитом `message`; ветка сливается в
    текущую ветку основной копии `merge --no-ff` (в истории видно, что это работа
    субагента); копия снимается, ветка удаляется. Конфликт — `merge --abort`, копия и
    ветка остаются: разбирать их — человеку или оркестратору.

    Returns:
        `{"branch", "merged"}` — ветка субагента и коммит слияния в основной копии.

    Raises:
        DshWorktreeError: в копии нечего сливать или слияние не прошло (уже откачено).
    """
    branch = _git(path, 'rev-parse', '--abbrev-ref', 'HEAD').strip()
    if dsh_worktree_dirty(path):
        _git(path, 'add', '-A')
        _git(path, 'commit', '-q', '-m', message)
    root = os.path.dirname(_git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip())
    if not _git(root, 'log', '--oneline', f'HEAD..{branch}').strip():
        raise DshWorktreeError(f'в ветке {branch} нет новых коммитов — сливать нечего')
    merged = subprocess.run(['git', '-C', root, 'merge', '--no-ff', '--no-edit', branch],
                            capture_output=True, text=True, timeout=DSH_WORKTREE_GIT_TIMEOUT)
    if merged.returncode != 0:
        _git(root, 'merge', '--abort', check=False)
        raise DshWorktreeError(f'слияние {branch} не прошло, откачено: {merged.stdout.strip()[-300:]}')
    dsh_worktree_drop(path)
    _git(root, 'branch', '-d', branch, check=False)
    return {'branch': branch, 'merged': _git(root, 'rev-parse', '--short', 'HEAD').strip()}


def dsh_worktree_drop(path: str, force: bool = False) -> None:
    """Убрать копию. Ветка остаётся — её сливают или удаляют руками.

    Args:
        force: убрать и с незакоммиченными правками (они пропадут).
    """
    args = ['worktree', 'remove'] + (['--force'] if force else []) + [path]
    _git(path, *args)


# ── детали реализации ──

def _start_ref(path: str) -> str:
    """Откуда копия ответвилась: ветка основной копии, на которой она была заведена."""
    common = _git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    head = _git(os.path.dirname(common), 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).strip()
    return head or 'HEAD@{upstream}'


def _slug(name: str) -> str:
    """Латинский слаг имени с меткой времени и случайным хвостом: ветки и каталоги не сталкиваются.

    ⚠ Одной метки времени мало: `fanout` заводит копии в одну секунду, а кириллическое имя
    целиком сводится к `task` — три задачи получали одну ветку, и две падали на старте.
    """
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:30] or 'task'
    return f'{base}-{time.strftime("%m%d-%H%M%S")}-{secrets.token_hex(2)}'


def _git(cwd: str, *args, check: bool = True) -> str:
    result = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True,
                            timeout=DSH_WORKTREE_GIT_TIMEOUT)
    if check and result.returncode != 0:
        raise DshWorktreeError(f'git {" ".join(args[:2])}: {result.stderr.strip()[:300]}')
    return result.stdout

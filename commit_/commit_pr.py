"""
Куда уехали коммиты ветки: база, PR-ы, осиротевшие коммиты — одним взглядом.

Коммит, сделанный в ветку, живёт в тревоге, пока не оказался в базе. Между
`git push` и `master` стоит PR, и он вправе слиться **раньше**, чем в ветку
приедут следующее: пуш в мёртвый PR не едет никуда, коммиты висят в закрытой
ветке, а знаешь ты об этом случайно. Так и было поймано: PR #1 слился на
`b22a95b`, два коммита позже остались в закрытой ветке мёртвыми — и это была
чистая случайность, что заметили.

Отсюда два вопроса, на которые модуль отвечает разом:

    git-вопрос:  какие коммиты ветки ещё не в базе и где они сейчас;
    gh-вопрос:   открыт ли PR ветки, на чём он закрылся, есть ли коммиты
                 старше и **после** его головы — осиротевшие.

Решение о втором PR тогда перестаёт быть случайностью: `--create` заведёт его
тем же домом — заголовок и тело собираются из сообщений коммитов (они и так
писались `<модуль>: <что и почему>`, см. `commit_style`).
"""
import argparse
import json
import subprocess
from pathlib import Path

# Базы в порядке доверия: своя ветка обязана сравниваться с общей веткой,
# а не с тем, что первый попадётся.
_COMMIT_PR_BASES = ('origin/master', 'master', 'origin/main', 'main')

# Что берём из `gh pr list` одним вызовом: `headRefOid` — голова PR, ей и
# сверяется «вошёл ли коммит в этот PR».
_COMMIT_PR_FIELDS = ('number,title,state,headRefName,headRefOid,mergedAt')


def commit_pr_view(branch: str = '', base: str = '', root: str = '.') -> dict:
    """Куда уехали коммиты текущей ветки.

    Args:
        branch: ветка; пусто — текущая.
        base: база (`origin/master`); пусто — первая найденная из
              `origin/master`, `master`, `origin/main`, `main`.
        root: каталог репозитория.

    Returns:
        {'branch', 'base', 'commits': [{'sha', 'subject', 'where'}],
         'prs': [...], 'orphan': bool} — `where` говорит, где коммит сейчас:
        в открытым PR, в слитом (база ещё не подтянута) или **ни в одном**
        (осиротел: ветка ушла дальше закрытого PR).

    ⚠ Осиротевший коммит — не «ждущий ревью»: закрытый PR его уже не пустит
    никуда, нужен **второй** PR, и молчание здесь дороже любой находки.
    """
    root = str(Path(root).resolve())
    branch = (branch or _git('rev-parse', '--abbrev-ref', 'HEAD', root)).strip()
    base = (base or next((one for one in _COMMIT_PR_BASES
                         if _exists(one, root)), '')).strip()

    prs = _prs(branch, root)
    commits = []

    for line in _git('log', '--format=%h\x1f%s', f'{base}..HEAD', root).splitlines():
        sha, _, subject = line.partition('\x1f')
        home = [pr for pr in prs if _ancestor(sha, pr['headRefOid'], root)]

        if not home:
            where = 'ни в одном PR ветки — ОСИРОТЕЛ, нужен второй PR'
        elif any(pr['state'] == 'OPEN' for pr in home):
            pr = next(pr for pr in home if pr['state'] == 'OPEN')
            where = f"в открытом PR #{pr['number']}"
        else:
            pr = home[0]
            where = f"в слитом PR #{pr['number']}, база ещё не подтянута"

        commits.append({'sha': sha, 'subject': subject, 'where': where})

    return {'branch': branch, 'base': base, 'commits': commits, 'prs': prs,
            'orphan': any('ОСИРОТЕЛ' in c['where'] for c in commits)}


def commit_pr_create(view: dict, root: str = '.') -> dict:
    """Завести второй PR для осиротевших коммитов: заголовок и тело — из них самих.

    ⚠ Тело — сообщения тех коммитов, что не доехали: они уже писались по
    стилу (`commit_style`), пересказывать их руками второй раз — путь
    к расхождению слов с делом.
    """
    root = str(Path(root).resolve())
    orphan = [c for c in view['commits'] if 'ОСИРОТЕЛ' in c['where']]
    if not orphan:
        raise RuntimeError('нечего везти: осиротевших коммитов нет')

    first = _git('log', '-1', '--format=%s', orphan[-1]['sha'], root)
    body = '\n'.join(f"- {c['sha']}: {c['subject']}\n{_bodies(c['sha'], root)}"
                     for c in orphan)
    out = _run(['gh', 'pr', 'create', '--title', first, '--body', body,
                '--head', view['branch'], '--base', view['base'].replace('origin/', '')])
    return {'url': out.strip()}


def commit_pr_format(view: dict) -> str:
    """Исход словами: где каждый коммит ветки и что с этим делать."""
    lines = [f"ветка `{view['branch']}` против `{view['base']}`: "
             f"не доехало коммитов {len(view['commits'])}, "
             f"PR ветки: {len(view['prs']) or 'нет'}"]
    for one in view['commits']:
        lines.append(f"  {one['sha']}  {one['where']}: {one['subject'][:60]}")
    for pr in view['prs']:
        closed = f", закрыт {pr['mergedAt'][:10]}" if pr['mergedAt'] else ''
        lines.append(f"  · PR #{pr['number']} «{pr['title'][:48]}» — "
                     f"{pr['state'].lower()}{closed}")
    if view['orphan']:
        lines.append('⚠ часть коммитов не доедет никуда, пока не заведён второй PR '
                     '(`--create`)')
    return '\n'.join(lines)


def _prs(branch: str, root: str) -> list:
    """PR этой ветки, любой судьбы: открытый, слитый, закрытый.

    ⚠ `--state all`: трагедия как раз в том, что PR **закрыт**, а память
    подсказывает только про открытые.
    """
    raw = _run(['gh', 'pr', 'list', '--state', 'all', '--limit', '50',
                '--json', _COMMIT_PR_FIELDS], root, allow_fail=True)
    try:
        got = json.loads(raw or '[]')
    except ValueError as err:
        raise RuntimeError(f'gh ответил не JSON: {raw[:120]}') from err
    return [pr for pr in got if pr.get('headRefName') == branch]


def _exists(ref: str, root: str) -> bool:
    """Есть ли такая ссылка в этом репозитории вовсе."""
    got = _run(['git', 'rev-parse', '--verify', '--quiet', ref], root,
               allow_fail=True, code=True)
    return got == 0


def _ancestor(sha: str, ref: str, root: str) -> bool:
    """Входит ли коммит в голову PR/базы.

    ⚠ Пустой `ref` (ветка без PR) — не «не входит», а «сверить нечем»: такой
    коммит считается не покрытым, и это верно.
    """
    return bool(ref) and _run(['git', 'merge-base', '--is-ancestor', sha, ref],
                              root, code=True) == 0


def _bodies(sha: str, root: str) -> str:
    """Тело сообщения коммита — оно и есть «почему», пересказывать нечего."""
    return '\n'.join('  ' + one for one in
                     _git('log', '-1', '--format=%b', sha, root).splitlines())


def _git(*args) -> str:
    """git, чей вывод нужен; последним аргументом — каталог репо."""
    *git_args, root = args
    return _run(['git', '-C', root, *git_args])


def _run(argv, root='', code: bool = False, allow_fail: bool = False):
    """Команда; `code` — только код возврата, иначе stdout.

    ⚠ `shell=False`, списком: сюда не приходит кавычек на три слоя, как в
    `ssh_x`, всё уже разборные аргументы.
    """
    got = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                         cwd=root or None)
    if code:
        return got.returncode
    if got.returncode and not allow_fail:
        raise RuntimeError(f'{" ".join(argv[:3])}…: {got.stderr.strip()[:200]}')
    if got.returncode:
        return None
    return got.stdout


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Куда уехали коммиты ветки: база, PR-ы, осиротевшие.')
    ap.add_argument('--branch', default='', help='ветка; пусто — текущая')
    ap.add_argument('--base', default='', help='база; пусто — первая найденная')
    ap.add_argument('--root', default='.', help='каталог репозитория')
    ap.add_argument('--create', action='store_true',
                    help='завести второй PR для осиротевших коммитов')
    args = ap.parse_args()

    try:
        view = commit_pr_view(args.branch, args.base, args.root)
        print(commit_pr_format(view))
        if args.create:
            print('заведён PR: ' + commit_pr_create(view, args.root)['url'])
        raise SystemExit(1 if view['orphan'] else 0)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

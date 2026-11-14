"""
Фича в базе и синки dev/staging — одна команда вместо «пяти движений».

Порядок на каждый прод-репозиторий каждый раз одинаковый: PR фичевой ветки
в `master`, merge, и ещё два синк-PR'а `master → dev` и `master → staging`,
иначе среды отстанут и следующий deploy понесёт чужое. Вручную это десять
вызовов gh на правку, а gh к api.github.com то и дело теряет коннект
(i/o timeout) — ручные повторы достали. Модуль проходит всю цепочку сам,
с retry на сетевых промахах.

## Что здесь

* `commit_pr_ship(root, title, body, base='master', sync=('dev','staging'), dry=False)`
  — пуш ветки, PR в базу, merge, синк-PR'ы базы в каждую среду; вернуть
  {'pr','merge_commit','sync': {среда: {'pr','state'}}}.

⚠ Мержить только merge-commit'ом (`--merge --admin`): squash переписывает
историю, и следующий синк `master → dev` конфликтует с уже влитым.
⚠ В dev/staging push'а нет — слияние идёт через временную ветку
`merge/<base>-into-<среда>`, сама среда меняется только merge'ом PR'а.
⚠ PR'ы создаёт `gh`, значит нужен авторизованный gh в этом каталоге;
на сетевой промах команда повторяется до трёх раз.

CLI (из каталога репозитория):

    python3 -m commit_.commit_pr_ship --title "helm: remove zombie queues job" \\
        --body "drops dod.* and replenishment.* on every deploy"
    # план без действий
    python3 -m commit_.commit_pr_ship --dry
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

# protected-ветки: из них не шипят, в них не толкают напрямую
_COMMIT_PR_SHIP_PROTECTED = ('master', 'main', 'dev', 'staging')
_RETRY = 3


def commit_pr_ship(root: str = '.', title: str = '', body: str = '',
                   base: str = 'master', sync=('dev', 'staging'),
                   dry: bool = False) -> dict:
    """Пройти всю цепочку: фича → base → синки в среды.

    Args:
        root: каталог репозитория.
        title: заголовок PR; пусто — последний коммит ветки.
        body: тело PR.
        base: база (`master`).
        sync: среды для синка базы; пустой список — только PR в базу.
        dry: True — только напечатать шаги, ничего не делать.

    Returns:
        {'branch','pr','merge_commit','sync': {среда: {'pr','state'}}};
        state синка — 'merged' либо 'already' (среда уже содержит базу).

    Raises:
        RuntimeError: грязное дерево, protected-ветка или gh/git отказал
                      после трёх попыток.
    """
    root = str(Path(root).resolve())
    branch = _git(root, 'rev-parse', '--abbrev-ref', 'HEAD')
    if branch in _COMMIT_PR_SHIP_PROTECTED:
        raise RuntimeError(f'из {branch!r} не шипят — checkout feature-ветки')
    if _git(root, 'status', '--porcelain'):
        raise RuntimeError('грязное дерево — закоммить или отдай его')
    if dry:
        return _dry_plan(root, branch, base, sync)

    title = title or _git(root, 'log', '-1', '--format=%s')
    _retry(['git', 'push', '-u', 'origin', branch], cwd=root)

    pr = _pr_ensure(root, branch, base, title, body)
    _gh(root, 'pr', 'merge', str(pr), '--merge', '--admin')
    commit = _pr_merged(root, pr)

    out = {'branch': branch, 'pr': pr, 'merge_commit': commit[:9], 'sync': {}}
    for target in sync:
        out['sync'][target] = _sync_one(root, base, target, dry=False)
    _git(root, 'checkout', branch)  # вернуться куда пришли
    return out


def _sync_one(root: str, base: str, target: str, dry: bool = False) -> dict:
    """Синк base → среда: временная ветка, merge, PR, merge PR'а."""
    tmp = f'merge/{base}-into-{target}'
    _retry(['git', 'fetch', 'origin', target, base], cwd=root)
    _git(root, 'checkout', '-B', tmp, f'origin/{target}')
    got = subprocess.run(['git', 'merge', '--no-edit', f'origin/{base}'],
                         cwd=root, capture_output=True, text=True)
    text = got.stdout + got.stderr
    if got.returncode != 0:
        raise RuntimeError(f'конфликт синка {base}→{target}: {text[-300:]}')
    if 'Already up to date' in text:
        return {'pr': None, 'state': 'already'}
    if dry:
        return {'pr': None, 'state': 'dry'}
    _retry(['git', 'push', '-f', 'origin', tmp], cwd=root)
    pr = _pr_ensure(root, tmp, target, f'Merge {base} into {target}',
                    f'Sync {base} into {target} (branch law).')
    _gh(root, 'pr', 'merge', str(pr), '--merge', '--admin')
    _pr_merged(root, pr)
    return {'pr': pr, 'state': 'merged'}


def _dry_plan(root: str, branch: str, base: str, sync) -> dict:
    for target in ('', *sync):
        step = (f'gh pr create --base {base} --head {branch} && gh pr merge --merge --admin'
                if target == '' else
                f'git checkout -B merge/{base}-into-{target} origin/{target}; '
                f'git merge origin/{base}; push; PR → merge')
        print('· ' + (step if not target else f'синк в {target}: ' + step))
    return {'branch': branch, 'pr': None, 'merge_commit': '', 'sync': {}}


def _pr_ensure(root: str, head: str, base: str, title: str, body: str) -> int:
    """Номер открытого PR head→base; нет — завести. Повторный create не беда."""
    got = subprocess.run(['gh', 'pr', 'list', '--head', head, '--base', base,
                          '--state', 'open', '--json', 'number'],
                         cwd=root, capture_output=True, text=True)
    if got.returncode == 0 and got.stdout.strip() != '[]':
        return json.loads(got.stdout)[0]['number']
    try:
        _gh(root, 'pr', 'create', '--head', head, '--base', base,
            '--title', title, '--body', body or '.')
    except RuntimeError as exc:
        # гонка/прошлое создание: PR уже есть — берём его номер
        if 'already exists' not in str(exc):
            raise
    got = _gh(root, 'pr', 'view', '--head', head, '--base', base, '--json', 'number')
    return json.loads(got)['number']


def _pr_merged(root: str, pr: int, timeout: int = 60) -> str:
    """Дождаться MERGED и вернуть sha merge-коммита."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = json.loads(_gh(root, 'pr', 'view', str(pr),
                               '--json', 'state,mergeCommit'))
        if state['state'] == 'MERGED':
            return (state.get('mergeCommit') or {}).get('oid', '')
        time.sleep(2)
    raise RuntimeError(f'PR #{pr} не слился за {timeout}с')


def _gh(root: str, *argv: str) -> str:
    return _retry(['gh', *argv], cwd=root)


def _git(root: str, *argv: str) -> str:
    return _retry(['git', *argv], cwd=root)


def _retry(argv: list, cwd: str, attempts: int = _RETRY) -> str:
    """gh/git с retry: i/o timeout к api.github.com здесь рядовое явление."""
    for n in range(1, attempts + 1):
        got = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        if got.returncode == 0:
            return got.stdout
        if n < attempts:
            time.sleep(4 * n)
    raise RuntimeError(f"{' '.join(argv)} -> {(got.stderr or got.stdout).strip()[-300:]}")


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('--root', default='.')
    _P.add_argument('--title', default='')
    _P.add_argument('--body', default='')
    _P.add_argument('--base', default='master')
    _P.add_argument('--sync', default='dev,staging', help='среды синка; пусто — только база')
    _P.add_argument('--dry', action='store_true')
    _C = _P.parse_args()
    try:
        res = commit_pr_ship(_C.root, _C.title, _C.body, _C.base,
                             tuple(s for s in _C.sync.split(',') if s), _C.dry)
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')
    if not _C.dry:
        print(f"{res['branch']} → PR #{res['pr']} смержен ({res['merge_commit']})")
        for target, one in res['sync'].items():
            print(f"  синк {target}: " +
                  (f"PR #{one['pr']} смержен" if one['state'] == 'merged'
                   else 'уже содержит базу' if one['state'] == 'already' else one['state']))
    raise SystemExit(0)

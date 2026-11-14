"""
Релиз тегом до hold: следующий тег, аннотированная метка на master, ожидание пайплайна.

Продакшен выкладывается push'ем тега (`v3.1.72`): CircleCI поднимает теговый
пайплайн, доходит до `hold-prod` и **ждёт ручного одобрения**. Ручная часть
повторялась каждый раз одинаково и весь прошлый релиз: сочинить следующий тег
по `git tag`, поставить аннотированную метку на master, push, а потом
вслепую опрашивать API — «пайплайн-то теговый, фильтром по ветке не
находится». Здесь это один вызов; одобрение остаётся ручным (кнопка у хозяина).

## Что здесь

* `circleci_release_next_tag(root)` — следующий `vMAJOR.MINOR.PATCH` по тегам origin;
* `circleci_release_tag(root, tag)` — аннотированная метка на `origin/master` + push;
* `circleci_release_wait(slug, tag)` — опрос тегового пайплайна до `hold`
  (встал на одобрение), `success` или `failed`.

⚠ Тег ставится на **`origin/master`**, а не на локальную голову: локаль может
отставать, а релизовать надо именно то, что в базе.
⚠ Workflow в `on_hold` — это не зависший и не упавший: `circleci_watch` такое
не различает и висит до таймаута. Здесь `on_hold` — отдельный исход `hold`.

CLI:

    # какой тег будет следующим
    python3 -m circleci_.circleci_release next --repo /var/www/cashier-service-accounts
    # тег + push + ждать, пока встанет на hold (или упадёт/сядет)
    python3 -m circleci_.circleci_release ship --repo /var/www/cashier-service-accounts
    # просто ждать уже запушенного тега
    python3 -m circleci_.circleci_release wait --repo /var/www/cashier-service-accounts --tag v3.1.72

Исод словами в конец: `RESULT=SUCCESS` | `RESULT=HOLD_WAITING_APPROVAL` |
`RESULT=FAILED pipeline=...` | `RESULT=TIMEOUT` (коды выхода 0/5/4/3).
"""
import argparse
import re
import subprocess
import time

from circleci_.circleci_api import circleci_api_latest_jobs, circleci_api_slug

# semver-теги вида v1.2.3 (суффиксов релизных тегов у проекта нет)
_TAG_RX = re.compile(r'^refs/tags/(v?)(\d+)\.(\d+)\.(\d+)\^?$')

_FAILED = ('failed', 'errored', 'timed_out')
_WAIT = 30


def circleci_release_next_tag(repo_dir: str = '.', prefix: str = '') -> str:
    """Следующий тег по тегам origin: max semver + 1 к patch.

    Args:
        repo_dir: каталог репозитория.
        prefix: обязательный префикс (`v`); теги без него не участвуют.

    Returns:
        строка вида `v3.1.73`.
    """
    best = (0, 0, 0, '', 'v')
    for ref in _ls_remote(repo_dir):
        m = _TAG_RX.match(ref)
        if m and (prefix == '' or m.group(1) == prefix):
            got = (int(m.group(2)), int(m.group(3)), int(m.group(4)), ref, m.group(1))
            if got[:3] > best[:3]:
                best = got
    if best[:3] == (0, 0, 0):
        raise RuntimeError('на origin нет ни одного semver-тега — префикс неверный?')
    major, minor, patch, _, mark = best
    return f'{mark}{major}.{minor}.{patch + 1}'


def circleci_release_tag(repo_dir: str = '.', tag: str = '', message: str = '',
                        ref: str = 'refs/heads/master') -> dict:
    """Поставить аннотированный тег на голову ветки (по умолчанию origin/master) и push.

    Args:
        repo_dir: каталог репозитория.
        tag: имя; пусто — `circleci_release_next_tag`.
        message: комментарий тега; пусто — имя.
        ref: реф-источник (`refs/heads/master` и т.п.).

    Returns:
        {'tag', 'sha'}.

    Raises:
        RuntimeError: тег уже есть на origin или push не прошёл.
    """
    refs = _ls_remote(repo_dir)
    tag = tag or circleci_release_next_tag(repo_dir)
    if f'refs/tags/{tag}' in refs:
        raise RuntimeError(f'тег {tag} уже на origin — следующий через '
                           f'circleci_release_next_tag или задать явно')
    parts = ref.split('/')
    sha = refs.get(ref) or refs.get(f'refs/heads/{ref}') or \
        next((s for r, s in refs.items() if r.endswith('/' + parts[-1])), '')
    if not sha:
        raise RuntimeError(f'рефа {ref} на origin нет')
    _run(['git', '-C', repo_dir, 'tag', '-a', tag, sha, '-m', message or tag])
    _run(['git', '-C', repo_dir, 'push', 'origin', tag])
    return {'tag': tag, 'sha': sha}


def circleci_release_wait(slug: str, tag: str, timeout: int = 1800,
                          interval: int = _WAIT, until: str = 'hold') -> dict:
    """Ждать теговый пайплайн тега.

    Args:
        slug: `gh/org/repo`.
        tag: имя тега — он же «ветка» пайплайна для API.
        timeout: потолок ожидания, сек.
        interval: пауза между опросами, сек.
        until: `hold` — остановиться на одобрении; `done` — идти до конца
               (hold при этом пережидается).

    Returns:
        {'state': 'hold'|'success'|'failed'|'timeout', 'number', 'workflows'}.
    """
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = circleci_api_latest_jobs(slug, branch=tag)
        if last:
            statuses = [w['status'] for w in last['workflows'].values()]
            # `last` первым, `state` после: у latest_jobs свой 'state' пайплайна
            # ('created'/'executing'), он не должен затирать вычисленный исход
            if any(s in _FAILED for s in statuses) or last['state'] in _FAILED:
                return {**last, 'state': 'failed'}
            if all(s == 'success' for s in statuses):
                return {**last, 'state': 'success'}
            if 'on_hold' in statuses and until == 'hold':
                return {**last, 'state': 'hold'}
        time.sleep(interval)
    return {**last, 'state': 'timeout'}


def _ls_remote(repo_dir: str) -> dict:
    """ref -> sha с origin одной командой (`ls-remote --refs`)."""
    out = _run(['git', '-C', repo_dir, 'ls-remote', '--refs', 'origin'])
    refs = {}
    for line in out.splitlines():
        sha, _, ref = line.partition('\t')
        refs[ref.strip()] = sha.strip()
    return refs


def _run(argv: list) -> str:
    got = subprocess.run(argv, capture_output=True, text=True)
    if got.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} -> {got.stderr.strip()[:200]}")
    return got.stdout


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('next', 'ship', 'wait'))
    _P.add_argument('--repo', default='.', help='каталог репозитория')
    _P.add_argument('--tag', default='', help='имя тега; пусто — следующий')
    _P.add_argument('--until', choices=('hold', 'done'), default='hold')
    _P.add_argument('--timeout', type=int, default=1800)
    _C = _P.parse_args()
    if _C.cmd == 'next':
        print(circleci_release_next_tag(_C.repo))
        raise SystemExit(0)
    slug = circleci_api_slug(_C.repo)
    tag = _C.tag
    if _C.cmd == 'ship':
        tag = circleci_release_tag(_C.repo, tag).get('tag') or tag
        print(f'тег {tag} запушен, жду пайплайн {slug}…')
    elif not tag:
        raise SystemExit('wait требует --tag')
    got = circleci_release_wait(slug, tag, _C.timeout, until=_C.until)
    for name, wf in (got.get('workflows') or {}).items():
        print(f'  workflow {name}: {wf["status"]}')
    code = {'success': 0, 'hold': 5, 'failed': 4, 'timeout': 3}[got['state']]
    end = {'success': 'SUCCESS', 'hold': 'HOLD_WAITING_APPROVAL',
           'failed': f"FAILED pipeline={got.get('number')}",
           'timeout': 'TIMEOUT'}[got['state']]
    print(f'RESULT={end} tag={tag} pipeline={got.get("number", "?")}')
    raise SystemExit(code)

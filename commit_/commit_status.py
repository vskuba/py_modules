"""
Статус-чеки коммита: живой ли CI-прогон этого кода и где он сейчас.

Дешёвый оракул для момента «запушил — чем ответили». Дорогой путь — API самой CI
(`circleci_`, Jenkins-логи, …): авторизация, uuid, цепочки вызовов. Дешёвый —
GitHub, которому CI сам докладывает по ходу: `gh api
repos/<org>/<repo>/commits/<sha>/status` и `/check-runs`. Отвечает на три
вопроса без единого знания о том, что за CI за ними:

* идёт ли прогон вообще (pending — CI может вообще не поднять push: фильтры
  веток, setup-стадия, очередь);
* какой чек чем кончил (`build-dev failure` — уже адрес, куда идти);
* свежий ли это прогон: сверка sha с головой ветки (`git ls-remote`).

⚠ Статус-чек — чужое заявление, а не факт: свойство «галка стоит», а не
«деплой живой». За фактом — `deploy_.deploy_wait`, за разбором CI — `circleci_`.
⚠ Два источника, и у старого API (`status`) нет `check-runs` от приложений
типа setup-пайплайнов: читают **оба**, один без другого враньём кончается.
"""
import argparse
import json
import subprocess


def _gh(path: str) -> dict:
    try:
        out = subprocess.run(['gh', 'api', path], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError('gh не установлен')
    except subprocess.CalledProcessError as err:
        raise RuntimeError(f'gh api {path}: {err.stderr.strip()[:200]}')
    return json.loads(out.stdout)


def _repo_path(repo_dir: str = '.') -> str:
    url = subprocess.run(['git', '-C', repo_dir, 'remote', 'get-url', 'origin'],
                         capture_output=True, text=True, check=True).stdout.strip()
    for sep in ('github.com:', 'github.com/'):
        if sep in url:
            return '/' + url.split(sep, 1)[1].removesuffix('.git').strip('/')
    raise RuntimeError(f'в remote нет github.com: {url}')


def commit_status_checks(sha: str, repo: str = '', repo_dir: str = '.') -> dict:
    """Все статус-чеки и check-runs коммита.

    Args:
        sha: полный или коротких хеш — GitHub примет.
        repo: `org/repo`; пусто — из git-remote `repo_dir`.
        repo_dir: каталог клона для взятия remote.

    Returns:
        `{'sha', 'state' (overall статуса), 'checks': {context: {'state',
        'target_url'}}, 'branch_head' (голова ветки с этим коммитом или '')}`;
        `state` — `pending`, если CI ещё не доложил (не путай с «завалили»).
    """
    repo = repo or _repo_path(repo_dir).lstrip('/')
    st = _gh(f'repos/{repo}/commits/{sha}/status')
    checks = {c['context']: {'state': c['state'], 'target_url': c.get('target_url', '')}
              for c in st.get('statuses', [])}
    try:
        for c in _gh(f'repos/{repo}/commits/{sha}/check-runs').get('check_runs', []):
            concl = c.get('conclusion')
            checks[c['name']] = {
                'state': 'success' if concl == 'success' else
                         'failure' if concl else c['status'].lower(),
                'target_url': c.get('html_url', ''),
            }
    except RuntimeError:
        pass  # check-runs нет у древних enterprise — одного статуса хватает
    return {'sha': st.get('sha', sha), 'state': st.get('state', 'pending'),
            'checks': checks, 'branch_head': ''}


def commit_status_is_head(sha: str, branch: str, repo_dir: str = '.') -> bool:
    """Лежит ли коммит на голове ветки (на всякий случай через ls-remote, без gh)."""
    try:
        head = subprocess.run(['git', '-C', repo_dir, 'ls-remote', 'origin', f'refs/heads/{branch}'],
                              capture_output=True, text=True, check=True).stdout.split()
    except subprocess.CalledProcessError:
        raise RuntimeError(f'ls-remote {branch} не удался')
    return bool(head) and head[0].startswith(sha)


def _cli(argv=None):
    ap = argparse.ArgumentParser(description='Статус-чеки коммита.')
    ap.add_argument('sha', nargs='?', default='HEAD')
    ap.add_argument('--repo', default='')
    ap.add_argument('--branch', default='', help='сверить с головой ветки')
    a = ap.parse_args(argv)
    sha = subprocess.run(['git', 'rev-parse', a.sha], capture_output=True,
                         text=True, check=True).stdout.strip() if a.sha == 'HEAD' else a.sha
    got = commit_status_checks(sha, repo=a.repo)
    fresh = f" (голова {a.branch}: {'да' if commit_status_is_head(sha, a.branch) else 'НЕТ — прогон чужой'})" if a.branch else ''
    print(f"{got['sha'][:7]}: {got['state']}{fresh}")
    for ctx, c in sorted(got['checks'].items()):
        print(f"  {ctx}: {c['state']}")
    raise SystemExit(0 if got['state'] == 'success' else (3 if got['state'] == 'pending' else 4))


if __name__ == '__main__':
    _cli()

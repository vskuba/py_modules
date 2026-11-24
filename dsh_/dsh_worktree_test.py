"""`dsh_worktree_land`: ссылки наружу не коммитятся, работа сливается, копия снимается."""
import os
import subprocess

from dsh_.dsh_worktree import dsh_worktree_land, dsh_worktree_new


def _git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout


def test_land_skips_outward_links_and_merges(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'master')
    _git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'init')
    outside = tmp_path / 'host-venv'
    outside.mkdir()
    tree = dsh_worktree_new(str(repo), 'правка')
    copy = tree['path']
    (open(os.path.join(copy, 'work.txt'), 'w')).write('работа\n')
    os.symlink(outside, os.path.join(copy, '.venv'))                  # хостовая ссылка — не работа
    os.symlink('work.txt', os.path.join(copy, 'inner-link'))          # ссылка внутрь — работа
    env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
    old = os.environ.copy()
    os.environ.update(env)
    try:
        dsh_worktree_land(copy, 'работа субагента')
    finally:
        os.environ.clear()
        os.environ.update(old)
    files = _git(repo, 'ls-files').split()
    assert 'work.txt' in files and 'inner-link' in files
    assert '.venv' not in files
    assert not os.path.exists(copy)

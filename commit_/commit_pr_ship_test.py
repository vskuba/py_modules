"""
Тесты публичных помощников выкладки: повтор до успеха, номер PR без гонки.

Сеть — подмена: `subprocess.run` и `time.sleep` модуля, живых gh/git и api.github.com
тест не трогает.
"""
from collections import namedtuple

import pytest

from commit_ import commit_pr_ship as m

Done = namedtuple('Done', 'returncode stdout stderr')


@pytest.fixture
def no_wait(monkeypatch):
    """Паузы повтора — в список, не в реальный сон."""
    sleeps = []
    monkeypatch.setattr(m.time, 'sleep', lambda s: sleeps.append(s))
    return sleeps


def test_retry_gives_stdout_after_failures(monkeypatch, no_wait):
    """Два i/o промаха, третья попытка удалась: stdout есть, пауз — две."""
    bag = [(1, '', 'dial tcp: i/o timeout'), (1, '', 'dial tcp: i/o timeout'), (0, 'ok-out', '')]

    def run(argv, cwd=None, capture_output=False, text=False):
        code, out, err = bag.pop(0)
        return Done(code, out, err)

    monkeypatch.setattr(m.subprocess, 'run', run)
    assert m.commit_pr_ship_retry(['git', 'push'], cwd='.') == 'ok-out'
    assert len(no_wait) == 2 and len(bag) == 0


def test_retry_raises_with_command_and_tail(monkeypatch, no_wait):
    """Все попытки мимо: `RuntimeError` с командой и хвостом stderr."""
    def run(argv, cwd=None, capture_output=False, text=False):
        return Done(1, '', 'could not read from remote repository')

    monkeypatch.setattr(m.subprocess, 'run', run)
    with pytest.raises(RuntimeError) as bad:
        m.commit_pr_ship_retry(['git', 'push', '-u', 'origin', 'br'], cwd='.', attempts=2)
    assert 'git push -u origin br' in str(bad.value)
    assert 'remote repository' in str(bad.value)


def test_pr_ensure_takes_existing_without_create(monkeypatch):
    """Открытый PR head→base уже есть — create не зовём вовсе."""
    def run(argv, cwd=None, capture_output=False, text=False):
        assert argv[:3] == ['gh', 'pr', 'list']
        assert '--state' in argv and 'open' in argv
        return Done(0, '[{"number": 7}]', '')

    monkeypatch.setattr(m.subprocess, 'run', run)
    assert m.commit_pr_ship_pr_ensure('.', 'br', 'master', 't', 'b') == 7


def test_pr_ensure_creates_when_absent(monkeypatch, no_wait):
    """PR нет — создаём и берём номер list'ом без state (view не умеет --head)."""
    calls = []

    def run(argv, cwd=None, capture_output=False, text=False):
        calls.append(' '.join(argv[:3]))
        if argv[:3] == ['gh', 'pr', 'list']:
            return Done(0, '[]' if 'open' in argv else '[{"number": 12}]', '')
        assert argv[:3] == ['gh', 'pr', 'create']
        return Done(0, 'https://github.com/o/r/pull/12', '')

    monkeypatch.setattr(m.subprocess, 'run', run)
    assert m.commit_pr_ship_pr_ensure('.', 'br', 'master', 't', 'body') == 12
    assert 'gh pr create' in calls


def test_pr_ensure_races_already_exists(monkeypatch, no_wait):
    """Гонка: create говорит «already exists» — номер берём из list, не падаем."""
    def run(argv, cwd=None, capture_output=False, text=False):
        if argv[:3] == ['gh', 'pr', 'create']:
            return Done(1, '', 'pull request already exists for branch "br"')
        return Done(0, '[]' if 'open' in argv else '[{"number": 5}]', '')

    monkeypatch.setattr(m.subprocess, 'run', run)
    assert m.commit_pr_ship_pr_ensure('.', 'br', 'master', 't', 'b') == 5


def test_pr_ensure_propagates_real_failure(monkeypatch, no_wait):
    """create мимо не по гонке — наружу, с текстом gh."""
    def run(argv, cwd=None, capture_output=False, text=False):
        if argv[:3] == ['gh', 'pr', 'list']:
            return Done(0, '[]', '')
        return Done(1, '', 'gh: HTTP 403 Resource not accessible by integration')

    monkeypatch.setattr(m.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='HTTP 403'):
        m.commit_pr_ship_pr_ensure('.', 'br', 'master', 't', 'b')


def test_helpers_are_public_not_private():
    """Правило имен: помощники публичные, старых приватных имен не осталось."""
    assert callable(m.commit_pr_ship_retry) and callable(m.commit_pr_ship_pr_ensure)
    assert not hasattr(m, '_retry') and not hasattr(m, '_pr_ensure')

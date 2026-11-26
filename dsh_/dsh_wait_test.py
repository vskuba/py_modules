"""Тесты `dsh_wait`: метка «ждём ответ» снимается любым забором ответа, ждуны не копятся.

`DSH_HOME` — в `tmp_path`, `~/.dsh` не трогаем; хост DSH не нужен.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from dsh_ import dsh_board, dsh_wait


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('DSH_HOME', str(tmp_path))
    return tmp_path


def test_inbox_take_drops_wait_mark(home):
    """Ответ забрал не ждун (`answer`, `inbox --take`) — метка снята, ждун не висит 12 ч."""
    dsh_wait.dsh_wait_expect('continuous-aaa', owner='orch-1')
    assert dsh_wait.dsh_wait_pending('orch-1') == ['continuous-aaa']

    dsh_board.dsh_board_inbox_take('continuous-aaa-g2', 3)

    assert dsh_wait.dsh_wait_pending('orch-1') == []


def test_wait_run_exits_when_answer_was_taken_elsewhere(home, monkeypatch):
    """Чужой забор ответа не держит ждуна: меток нет — он выходит сразу и будит оркестратора."""
    dsh_wait.dsh_wait_expect('continuous-bbb', owner='orch-2')
    dsh_board.dsh_board_inbox_take('continuous-bbb', 1)
    monkeypatch.setattr(dsh_wait, 'dsh_wait_answered', lambda *a, **k: [])

    started = time.time()
    taken = dsh_wait.dsh_wait_run('orch-2', str(home / 'out'), timeout=60)

    assert taken == [] and time.time() - started < 5
    assert not os.path.exists(dsh_wait._waiter_path('orch-2'))


def test_expired_marks_are_pruned_on_next_edit(home):
    """Метки старше `DSH_WAIT_MAX_AGE` из файла уходят, а не копятся сутками."""
    path = home / dsh_wait.DSH_WAIT_FILE
    path.write_text(json.dumps({
        'continuous-old': {'owner': 'x', 'at': time.time() - dsh_wait.DSH_WAIT_MAX_AGE - 60},
        'continuous-bad': 'not-a-row',
    }))

    dsh_wait.dsh_wait_expect('continuous-new', owner='orch-3')

    assert set(json.loads(path.read_text())) == {'continuous-new'}


def test_new_waiter_replaces_previous_waiter_of_same_session(home):
    """Второй ждун сессии гасит первого: будить первому уже некого."""
    old = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'dsh-board', 'wait'])
    try:
        (home / os.path.basename(dsh_wait._waiter_path('orch-4'))).write_text(str(old.pid))

        dsh_wait._waiter_replace('orch-4')

        assert old.wait(timeout=5) != 0
    finally:
        old.kill()


def test_waiter_replace_spares_foreign_process_with_reused_pid(home):
    """pid из файла достался чужому процессу — его не трогаем."""
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        (home / os.path.basename(dsh_wait._waiter_path('orch-5'))).write_text(str(other.pid))

        dsh_wait._waiter_replace('orch-5')

        time.sleep(0.3)
        assert other.poll() is None
    finally:
        other.kill()

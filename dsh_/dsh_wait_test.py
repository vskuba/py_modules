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


# ─── ожидание названной карточки ───

def _answered_only(want):
    """Подмена «есть ли ответ»: отвечает только карточка `continuous-fast`, и то не всякий раз."""
    def answered(sessions, **_kw):
        return [{'session': 'continuous-fast', 'title': 'быстрая', 'turns': 2}] \
            if 'continuous-fast' in sessions else []
    return answered


def test_wait_of_named_card_exits_before_the_slow_one(home, monkeypatch):
    """Ждун одной карточки будит оркестратора её именем, не дожидаясь самой медленной."""
    from dsh_ import dsh_board, dsh_session
    dsh_wait.dsh_wait_expect('continuous-slow', owner='orch-6')
    dsh_wait.dsh_wait_expect('continuous-fast', owner='orch-6')
    monkeypatch.setattr(dsh_wait, 'dsh_wait_answered', _answered_only('fast'))
    monkeypatch.setattr(dsh_session, 'dsh_session_answer_last', lambda session, **kw: 'готово')
    monkeypatch.setattr(dsh_board, 'dsh_board_box_clear', lambda session, **kw: None)

    taken = dsh_wait.dsh_wait_run('orch-6', str(home / 'out'), timeout=5,
                                  sessions=['continuous-fast'], first=True, slot='continuous-fast')

    assert [row['session'] for row in taken] == ['continuous-fast']
    assert (home / 'out' / 'continuous-fast.md').read_text() == 'готово'
    # Медленная карточка осталась помеченной: её ждёт свой процесс, а не этот.
    assert dsh_wait.dsh_wait_pending('orch-6') == ['continuous-slow']


def test_per_card_waiters_do_not_extinguish_each_other(home):
    """Ждуны разных карточек одной сессии не гасят друг друга: ряд — часть имени файла."""
    first = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'dsh-board', 'wait'])
    try:
        (home / os.path.basename(dsh_wait._waiter_path('orch-7', 'continuous-a'))).write_text(str(first.pid))

        dsh_wait._waiter_replace('orch-7', 'continuous-b')
        time.sleep(0.3)
        assert first.poll() is None

        dsh_wait._waiter_replace('orch-7', 'continuous-a')
        assert first.wait(timeout=5) != 0
    finally:
        first.kill()


def test_waiter_alive_counts_a_per_card_waiter(home):
    """Заставе `Stop` достаточно ждуна любого ряда: карточка ждать поставлена, ход свободен."""
    waiter = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'dsh-board', 'wait'])
    try:
        (home / os.path.basename(dsh_wait._waiter_path('orch-8', 'continuous-a'))).write_text(str(waiter.pid))
        assert dsh_wait.dsh_wait_waiter_alive('orch-8')
    finally:
        waiter.kill()
        waiter.wait(timeout=5)                        # zombie: os.kill(pid, 0) на него отвечает «жив»
    assert not dsh_wait.dsh_wait_waiter_alive('orch-8')

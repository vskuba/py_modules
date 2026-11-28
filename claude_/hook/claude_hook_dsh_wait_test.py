"""Тесты заставы «ответ субагента не уходит в никуда»: покарточный ждун покрывает свой ряд.

`DSH_HOME` и `TMPDIR` — в `tmp_path`, `~/.dsh` не трогаем; ждун — живой короткий процесс с
файлом pid, хост для теста не нужен.
"""
import subprocess
import sys

import pytest

from claude_.hook import claude_hook_dsh_wait as gate
from dsh_ import dsh_board, dsh_session, dsh_wait

OWNER = 'orch-1'


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Дом хоста и временный каталог — в `tmp_path`, `~/.dsh` не трогаем."""
    monkeypatch.setenv('DSH_HOME', str(tmp_path))
    monkeypatch.setenv('TMPDIR', str(tmp_path / 'tmp'))
    return tmp_path


@pytest.fixture(autouse=True)
def _silent_host(monkeypatch):
    """Хост подставной и молчаливый: доски нет, карточек нет, сети нет — метки живые.

    ⚠ `dsh_board` связал `dsh_session_*` своими именами на импорте — правим и там, и там.
    """
    monkeypatch.setattr(dsh_board, 'dsh_board_state', lambda session='', **kw: {})
    monkeypatch.setattr(dsh_session, 'dsh_session_list', lambda **kw: [])
    monkeypatch.setattr(dsh_board, 'dsh_session_list', lambda **kw: [])
    monkeypatch.setattr(dsh_session, 'dsh_session_history', lambda session, span=2, **kw: [])
    monkeypatch.setattr(dsh_board, 'dsh_session_history', lambda session, span=2, **kw: [])


def _waiter(home, slot=''):
    """Живой процесс-ждун с файлом pid в названном ряду; тест гасит его сам."""
    waiter = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'dsh-board', 'wait'])
    with open(dsh_wait._waiter_path(OWNER, slot), 'w', encoding='utf-8') as handle:
        handle.write(str(waiter.pid))
    return waiter


def test_gate_holds_the_card_without_a_waiter(home):
    """Из двух меток под ждуном только одна: ход не отпущен, и названа непокрытая."""
    dsh_wait.dsh_wait_expect('continuous-aaaa1111', owner=OWNER)
    dsh_wait.dsh_wait_expect('continuous-bbbb2222', owner=OWNER)
    waiter = _waiter(home, slot='continuous-aaaa1111')
    try:
        text = gate.claude_hook_dsh_wait_check({'session_id': OWNER})
    finally:
        waiter.kill()
        waiter.wait(timeout=5)
    assert '1 шт. (bbbb2222)' in text                  # покрытая карточка в счёте не участвует
    assert 'aaaa1111' not in text


def test_gate_silent_when_general_waiter_alive(home):
    """Общий ждун ждёт все метки сессии — застава молчит."""
    dsh_wait.dsh_wait_expect('continuous-aaaa1111', owner=OWNER)
    waiter = _waiter(home)
    try:
        assert gate.claude_hook_dsh_wait_check({'session_id': OWNER}) == ''
    finally:
        waiter.kill()
        waiter.wait(timeout=5)


def test_gate_holds_when_waiter_pid_is_dead(home):
    """Файл pid, переживший перезапуск харнеса, ничего не покрывает: застава держит."""
    dsh_wait.dsh_wait_expect('continuous-aaaa1111', owner=OWNER)
    with open(dsh_wait._waiter_path(OWNER), 'w', encoding='utf-8') as handle:
        handle.write('999999999')                      # заведомо мёртвый pid
    assert '1 шт. (aaaa1111)' in gate.claude_hook_dsh_wait_check({'session_id': OWNER})


def test_gate_silent_without_marks(home):
    """Меток нет — хода застава не держит, даже когда ждун жив."""
    waiter = _waiter(home)
    try:
        assert gate.claude_hook_dsh_wait_check({'session_id': OWNER}) == ''
    finally:
        waiter.kill()
        waiter.wait(timeout=5)


def test_gate_frees_a_cancelled_and_archived_card(home, monkeypatch):
    """Карточку убрали (`cancel`+`archive`, `gc`) — мёртвая метка ход не держит и сама снимается."""
    card = 'continuous-dead0001'
    dsh_wait.dsh_wait_expect(card, owner=OWNER)
    monkeypatch.setattr(dsh_board, 'dsh_board_state',
                        lambda session='', **kw: {'archived': [card], 'goals': []})
    assert gate.claude_hook_dsh_wait_check({'session_id': OWNER}) == ''
    assert dsh_wait.dsh_wait_pending(OWNER) == []


def test_gate_silent_for_cancelled_card(home):
    """Отменённая карточка заставу не занимает: `cancel` снял метку — ждать нечего, ход свободен."""
    card = 'continuous-cccc0001'
    dsh_wait.dsh_wait_expect(card, owner=OWNER)
    assert '1 шт.' in gate.claude_hook_dsh_wait_check({'session_id': OWNER})   # до отмены бдит
    dsh_wait.dsh_wait_done(card)                       # как это делает `dsh-board cancel <карточка>`
    assert gate.claude_hook_dsh_wait_check({'session_id': OWNER}) == ''


def test_gate_silent_for_cancelled_card(home):
    """Отменённая карточка заставу не занимает: `cancel` снял метку — ждать нечего, ход свободен."""
    card = 'continuous-cccc0001'
    dsh_wait.dsh_wait_expect(card, owner=OWNER)
    assert '1 шт.' in gate.claude_hook_dsh_wait_check({'session_id': OWNER})   # до отмены бдит
    dsh_wait.dsh_wait_done(card)                       # как это делает `dsh-board cancel <карточка>`
    assert gate.claude_hook_dsh_wait_check({'session_id': OWNER}) == ''

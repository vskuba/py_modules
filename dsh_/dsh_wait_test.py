"""Тесты `dsh_wait`: метка «ждём ответ» снимается любым забором ответа, ждуны не копятся.

`DSH_HOME` — в `tmp_path`, `~/.dsh` не трогаем; хост DSH не нужен.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from dsh_ import dsh_board, dsh_session, dsh_wait
from dsh_.dsh_api import DshApiUnreachable


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('DSH_HOME', str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _silent_host(monkeypatch):
    """Хост подставной и молчаливый: доски нет, карточек нет, сети нет.

    Мёртвых карточек ждун в такой пустоте не находит ни у какой метки — старые тесты
    заглушка не меняет; нужная доска ставится самим тестом поверх этой заглушки.
    ⚠ `dsh_board` связал `dsh_session_*` своими именами на импорте — правим и там, и там.
    """
    monkeypatch.setattr(dsh_board, 'dsh_board_state', lambda session='', **kw: {})
    monkeypatch.setattr(dsh_session, 'dsh_session_list', lambda **kw: [])
    monkeypatch.setattr(dsh_board, 'dsh_session_list', lambda **kw: [])
    monkeypatch.setattr(dsh_session, 'dsh_session_history', lambda session, span=2, **kw: [])
    monkeypatch.setattr(dsh_board, 'dsh_session_history', lambda session, span=2, **kw: [])


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
    """Живость — по любому живому ждуну сессии; покрывает ли он метки — смотри `dsh_wait_waiter_gaps`."""
    waiter = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'dsh-board', 'wait'])
    try:
        (home / os.path.basename(dsh_wait._waiter_path('orch-8', 'continuous-a'))).write_text(str(waiter.pid))
        assert dsh_wait.dsh_wait_waiter_alive('orch-8')
    finally:
        waiter.kill()
        waiter.wait(timeout=5)                        # zombie: os.kill(pid, 0) на него отвечает «жив»
    assert not dsh_wait.dsh_wait_waiter_alive('orch-8')


# ─── покрытие меток: пробелы ожидания (для застав и хуков) ───

def test_per_card_waiter_covers_only_its_own_card(home):
    """Ждун карточки A не покрывает карточку B: B остаётся в пробеле, хотя ждун жив."""
    dsh_wait.dsh_wait_expect('continuous-a', owner='orch-9')
    dsh_wait.dsh_wait_expect('continuous-b', owner='orch-9')
    waiter = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'dsh-board', 'wait'])
    try:
        (home / os.path.basename(dsh_wait._waiter_path('orch-9', 'continuous-a'))).write_text(str(waiter.pid))
        assert dsh_wait.dsh_wait_waiter_gaps('orch-9') == ['continuous-b']
        assert dsh_wait.dsh_wait_waiter_alive('orch-9')      # живость — это одно, покрытие — другое
    finally:
        waiter.kill()
        waiter.wait(timeout=5)
    assert dsh_wait.dsh_wait_waiter_gaps('orch-9') == ['continuous-a', 'continuous-b']


def test_general_waiter_covers_every_mark(home):
    """Общий ждун (без ряда) покрывает все метки сессии; мёртвый — ничего."""
    dsh_wait.dsh_wait_expect('continuous-a', owner='orch-10')
    waiter = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'dsh-board', 'wait'])
    try:
        (home / os.path.basename(dsh_wait._waiter_path('orch-10'))).write_text(str(waiter.pid))
        assert dsh_wait.dsh_wait_waiter_gaps('orch-10') == []
    finally:
        waiter.kill()
        waiter.wait(timeout=5)
    assert dsh_wait.dsh_wait_waiter_gaps('orch-10') == ['continuous-a']


def test_wait_exits_when_marks_are_taken_during_the_wait(home, monkeypatch):
    """Метку сняли чужим забором, пока безымянный ждун спал: выйти сейчас, а не висеть 6 ч."""
    dsh_wait.dsh_wait_expect('continuous-ddd', owner='orch-11')
    calls = []

    def answered(sessions, **_kw):
        if not calls:                                  # первый виток: ответ забрал `dsh-board answer`
            calls.append(1)
            dsh_board.dsh_board_inbox_take('continuous-ddd', 1)
        return []

    monkeypatch.setattr(dsh_wait, 'dsh_wait_answered', answered)
    started = time.time()
    taken = dsh_wait.dsh_wait_run('orch-11', str(home / 'out'), timeout=60)
    assert taken == [] and time.time() - started < 5


# ─── мёртвые карточки и ответы не по метке (грабли 27–28.11) ───

def _board(monkeypatch, state=None, items=None, history=None):
    """Доска, список карточек и журналы — подставные (поверх `_silent_host`), сети нет."""
    all_items = lambda **kw: (items() if callable(items) else items) or []
    all_history = history or (lambda session, span=2, **kw: [])
    monkeypatch.setattr(dsh_board, 'dsh_board_state', lambda session='', **kw: state or {})
    monkeypatch.setattr(dsh_session, 'dsh_session_list', all_items)
    monkeypatch.setattr(dsh_board, 'dsh_session_list', all_items)   # `_inbox_scan` зовёт своё имя
    monkeypatch.setattr(dsh_session, 'dsh_session_history', all_history)
    monkeypatch.setattr(dsh_board, 'dsh_session_history', all_history)


def _item(key, running=False, turns=0, cwd='/srv/project', queue=0):
    """Карточка `session/list`; `queue` — сколько контента стоит в очереди следующего захода."""
    return {'sessionId': key, 'cwd': cwd, 'running': running, 'updatedAt': time.time() * 1000,
            'projections': {'values': {'title': key, 'sessionStats': {'turns': turns},
                                       'inbox': {'next-turn': [],
                                                 'next-step': [{'content': []}] * queue}}}}


def _end(at):
    """Хвост журнала: круг кончился в момент `at` обычным концом (не обрывом)."""
    return [{'type': 'turn/end', 'seq': 9, 'time': at * 1000, 'data': {'reason': {'kind': 'done'}}}]


def _took_fast(monkeypatch, text='готово'):
    """Забор ответа и уборка бокса — подставные; ждун забирает `text` в файл."""
    monkeypatch.setattr(dsh_session, 'dsh_session_answer_last', lambda session, after=0, **kw: text)
    monkeypatch.setattr(dsh_board, 'dsh_board_box_clear', lambda session, **kw: [])


def test_worktree_card_answer_wakes_the_waiter(home, monkeypatch):
    """Карточка копии (её cwd — технический workspace, не репозиторий) будит ждун как все."""
    card = 'continuous-wt00000001'
    dsh_wait.dsh_wait_expect(card, owner='orch-9')
    t0 = time.time()
    done = lambda: time.time() - t0 > 0.3
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_POLL', 0.05)
    _board(monkeypatch, state={'robots': [card], 'archived': [], 'goals': []},
           items=lambda: [_item(card, running=not done(), turns=int(done()),
                                cwd=str(home / 'dsh-worktrees'))],
           history=lambda session, span=2, **kw: _end(t0 + 0.3) if done() else [])
    _took_fast(monkeypatch)

    taken = dsh_wait.dsh_wait_run('orch-9', str(home / 'out'), timeout=10)

    assert [row['session'] for row in taken] == [card]
    assert (home / 'out' / f'{card}.md').read_text() == 'готово'
    assert dsh_wait.dsh_wait_pending('orch-9') == []


def test_stale_answer_of_previous_round_is_not_the_new_one(home, monkeypatch):
    """`ask` репликой: у карточки лежал непрочитанным ответ прошлого круга — ждун ждёт новый."""
    card = 'continuous-ask0000002'
    dsh_wait.dsh_wait_expect(card, owner='orch-2')      # метка — позже старого ответа
    t0 = time.time()
    done = lambda: time.time() - t0 > 0.3
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_POLL', 0.05)
    _board(monkeypatch, state={'robots': [card], 'archived': [],
                              'goals': [{'session': card, 'status': 'active'}]},  # новый круг в очереди
           items=lambda: [_item(card, turns=2 if done() else 1)],
           history=lambda session, span=2, **kw: _end(t0 + 0.3 if done() else t0 - 3600))
    monkeypatch.setattr(dsh_session, 'dsh_session_answer_last',
                        lambda session, after=0, **kw: 'второй круг' if done() else 'первый круг')
    monkeypatch.setattr(dsh_board, 'dsh_board_box_clear', lambda session, **kw: [])

    taken = dsh_wait.dsh_wait_run('orch-2', str(home / 'out'), timeout=10)

    assert [row['session'] for row in taken] == [card]
    assert (home / 'out' / f'{card}.md').read_text() == 'второй круг'   # не старый хвост карточки
    assert time.time() - t0 > 0.25                                      # дождались круга, а не хлама
    assert dsh_wait.dsh_wait_pending('orch-2') == []


def test_dead_cards_are_pruned_and_the_waiter_exits(home, monkeypatch):
    """Архивная и уснувшая без цели карточки не вешают ждун: метки сняты, отряд вышел."""
    live, archived, asleep = ('continuous-live00001', 'continuous-dead0001', 'continuous-sleep001')
    for card in (live, archived, asleep):
        dsh_wait.dsh_wait_expect(card, owner='orch-7')
    path = home / dsh_wait.DSH_WAIT_FILE
    rows = json.loads(path.read_text())
    for card in (archived, asleep):
        rows[card]['at'] = time.time() - dsh_wait.DSH_WAIT_DEAD_GRACE - 10
    path.write_text(json.dumps(rows))
    t0 = time.time()
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_POLL', 0.05)
    _board(monkeypatch, state={'robots': [live, archived, asleep], 'archived': [archived], 'goals': []},
           items=[_item(live, turns=1), _item(archived), _item(asleep)],
           history=lambda session, span=2, **kw: _end(t0) if session == live else [])
    _took_fast(monkeypatch)

    taken = dsh_wait.dsh_wait_run('orch-7', str(home / 'out'), timeout=30)

    assert [row['session'] for row in taken] == [live]
    assert dsh_wait.dsh_wait_pending('orch-7') == []     # мёртвые метки сняты, а не висят до 12 ч
    assert time.time() - t0 < 15                        # без правки — висел бы весь `timeout`


def test_queued_goal_keeps_a_sleeping_card_alive(home, monkeypatch):
    """Спит без круга, но цель на доске ждёт мотора — ответ будет, метку не хороним."""
    card = 'continuous-queue0001'
    _board(monkeypatch, state={'goals': [{'session': card, 'status': 'active'}]}, items=[_item(card)])
    assert dsh_wait.dsh_wait_unreachable([card], {card: time.time() - 7000}) == []


def test_silent_card_without_goal_is_dead(home, monkeypatch):
    """Спит, цели нет, после метки ни одного круга — ответа не будет: метка мертва."""
    card = 'continuous-slept0002'
    _board(monkeypatch, items=[_item(card, turns=1)],
           history=lambda session, span=2, **kw: _end(time.time() - 10000))
    assert dsh_wait.dsh_wait_unreachable([card], {card: time.time() - 7000}) == [card]


def test_cancel_and_archive_drop_the_wait_mark(home, monkeypatch):
    """`cancel`/`archive` сняли круг руками — метка снимается сразу, застава хода не держит."""
    from dsh_ import dsh_board_cli
    card = 'continuous-gone0001'
    for command in ('cancel', 'archive'):
        dsh_wait.dsh_wait_expect(card, owner='orch-1')
        # Сам хост подставной (сети нет); снятие метки — контракт самих `dsh_board_*`
        # (у cancel это делает и CLI, у archive — он внутри; проверено в `dsh_board_test`).
        monkeypatch.setattr(dsh_board_cli, f'dsh_board_{command}',
                            lambda session, **kw: (dsh_wait.dsh_wait_done(session),
                                                   {'stopped': False, 'removed': []})[1])
        assert dsh_board_cli.dsh_board_cli_main([command, card]) == 0
        assert dsh_wait.dsh_wait_pending('orch-1') == []


def test_queued_round_outlives_the_dead_grace(home, monkeypatch):
    """Все моторы заняты чужим: круг в очереди заходов — карточка жива, метку не хороним."""
    waiting, sleeping = 'continuous-queue0002', 'continuous-slept0003'
    _board(monkeypatch, state={'goals': []},
           items=[_item(waiting, queue=1), _item(sleeping)])
    at = time.time() - 7000                      # обе метки старше `DSH_WAIT_DEAD_GRACE`
    assert dsh_wait.dsh_wait_unreachable([waiting], {waiting: at}) == []
    assert dsh_wait.dsh_wait_unreachable([sleeping], {sleeping: at}) == [sleeping]


def test_queued_round_is_awaited_until_done(home, monkeypatch):
    """Карточка вне реестра `robots`: очередь → бег → ответ — ждун выходит на ответе.

    Два грабеля 28.11 в одном сюжете: пока круг ждал мотора, `running` ложь и журнал пуст
    — ждун не должен был ни посчитать его готовым, ни похоронить метку; а когда круг
    кончился, ответа во «входящих» нет (карточка не в `robots`) — ждун обязан найти его
    в журнале и выйти, а не висеть до `DSH_WAIT_MAX_AGE` (`d98f4f43`).
    """
    card = 'continuous-queue0003'
    dsh_wait.dsh_wait_expect(card, owner='orch-3')
    t0 = time.time()
    started, done = lambda: time.time() - t0 > 0.25, lambda: time.time() - t0 > 0.5
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_POLL', 0.05)
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_DEAD_GRACE', 0.0)   # проверка мёртвости — каждый виток
    _board(monkeypatch, state={'robots': [], 'archived': [], 'goals': []},   # «входящие» — пусты навсегда
           items=lambda: [_item(card, running=started() and not done(),
                                turns=int(done()), queue=int(not started()))],
           history=lambda session, span=2, **kw: _end(t0 + 0.5) if done() else [])
    _took_fast(monkeypatch)

    taken = dsh_wait.dsh_wait_run('orch-3', str(home / 'out'), timeout=10)

    assert [row['session'] for row in taken] == [card]
    assert (home / 'out' / f'{card}.md').read_text() == 'готово'
    assert dsh_wait.dsh_wait_pending('orch-3') == []
    assert time.time() - t0 > 0.5               # не будил ответом «из очереди» и не выходил раньше


def test_answer_of_unregistered_card_is_found_in_journal(home, monkeypatch):
    """Жданная карточка не в `robots`, а круг кончился после метки — ответ ищется в журнале."""
    card = 'continuous-journal001'
    t0 = time.time()
    _board(monkeypatch, state={'robots': [], 'archived': [], 'goals': []},
           items=[_item(card, turns=1)], history=lambda session, span=2, **kw: _end(t0))
    rows = dsh_wait.dsh_wait_answered([card], marked_at={card: t0 - 100})
    assert [row['session'] for row in rows] == [card] and rows[0]['turns'] == 1
    assert dsh_wait.dsh_wait_answered([card], marked_at={card: t0 + 100}) == []  # круг был до метки
    cut = [{'type': 'turn/end', 'seq': 9, 'time': t0 * 1000, 'data': {'reason': {'kind': 'interrupted'}}}]
    _board(monkeypatch, state={'robots': []}, items=[_item(card, turns=1)],
           history=lambda session, span=2, **kw: cut)
    assert dsh_wait.dsh_wait_answered([card], marked_at={card: t0 - 100}) == []  # обрыв продолжит `revive`
    _board(monkeypatch, state={'robots': []}, items=[_item(card, turns=1)],
           history=lambda session, span=2, **kw: _end(t0))
    dsh_board.dsh_board_inbox_take(card, 1)                        # ответ уже забран другим
    assert dsh_wait.dsh_wait_answered([card], marked_at={card: t0 - 100}) == []


def test_named_waiter_exits_when_card_was_cancelled(home, monkeypatch):
    """Отменённая карточка не вешает названного ждуна: снятая метка — сигнал, а не потеря.

    Ждун `wait <карточка>` держит список фиксированным; без учёта снятия метки отменённая
    (или забранная чужим забором) карточка висиела бы до `timeout` — ход оркестратора
    заперт карточкой, ответа от которой уже никто не ждёт.
    """
    card = 'continuous-cancel00001'
    dsh_wait.dsh_wait_expect(card, owner='orch-cancel')
    fired = []

    def answered(sessions, **_kw):
        if not fired:                        # имитируем параллельный `dsh-board cancel <card>`:
            fired.append(1)                  # круг прерван, очередь снята, метка убрана
            dsh_wait.dsh_wait_done(card)
        return []

    monkeypatch.setattr(dsh_wait, 'dsh_wait_answered', answered)
    monkeypatch.setattr(dsh_wait, 'dsh_wait_unreachable', lambda *a, **kw: [])
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_POLL', 0.05)

    started = time.time()
    taken = dsh_wait.dsh_wait_run('orch-cancel', str(home / 'out'), timeout=60, sessions=[card])

    assert taken == [] and fired and time.time() - started < 5   # не дождался бы и до timeout


def test_waiter_survives_flaky_host(home, monkeypatch):
    """Хост рвёт ответ посреди опроса — ждун пропускает виток и ждёт дальше, а не умирает.

    28.11: `IncompleteRead(0 bytes read)` по живому хосту (транспорт сворачивает его в
    `DshApiUnreachable`) вешал `wait` трейсбеком с кодом 1 — карточка оставалась неотвеченной.
    """
    card = 'continuous-flak00001'
    dsh_wait.dsh_wait_expect(card, owner='orch-flaky')
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_POLL', 0.05)
    monkeypatch.setattr(dsh_board, 'dsh_board_inbox',
                        lambda **kw: [{'session': card, 'title': card, 'turns': 1, 'brief': ''}])
    monkeypatch.setattr(dsh_wait, 'dsh_wait_unreachable', lambda *a, **kw: [])
    left = {'fails': 2}

    def history(session, span=2, **kw):
        if left['fails']:                          # два витка хост рвёт ответ, третий — отвечает
            left['fails'] -= 1
            raise DshApiUnreachable('хост не ответил: IncompleteRead(0 bytes read)')
        return _end(time.time())
    monkeypatch.setattr(dsh_session, 'dsh_session_history', history)
    _took_fast(monkeypatch)

    started = time.time()
    taken = dsh_wait.dsh_wait_run('orch-flaky', str(home / 'out'), timeout=60)

    assert [row['session'] for row in taken] == [card] and time.time() - started < 10


def test_waiter_retries_a_failed_take(home, monkeypatch):
    """Ответ не удалось забрать из-за сбоя — не потеря: забор повторится на следующем витке."""
    card = 'continuous-flak00002'
    dsh_wait.dsh_wait_expect(card, owner='orch-flaky2')
    monkeypatch.setattr(dsh_wait, 'DSH_WAIT_POLL', 0.05)
    monkeypatch.setattr(dsh_board, 'dsh_board_inbox',
                        lambda **kw: [{'session': card, 'title': card, 'turns': 1, 'brief': ''}])
    monkeypatch.setattr(dsh_wait, 'dsh_wait_unreachable', lambda *a, **kw: [])
    monkeypatch.setattr(dsh_session, 'dsh_session_history',
                        lambda session, span=2, **kw: _end(time.time()))
    _took_fast(monkeypatch)
    calls = {'n': 0}

    def answer_last(session, after=0, **kw):
        calls['n'] += 1
        if calls['n'] == 1:                        # забор ответа споткнулся о сетевой сбой —
            raise DshApiUnreachable('хост не ответил: таймаут')   # повторится на следующем витке
        return 'готово'
    monkeypatch.setattr(dsh_session, 'dsh_session_answer_last', answer_last)

    taken = dsh_wait.dsh_wait_run('orch-flaky2', str(home / 'out'), timeout=60)

    assert [row['session'] for row in taken] == [card]

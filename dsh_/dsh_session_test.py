"""Тесты чтения журнала карточки: пара «задача — ответ» долгого круга и границы кругов.

Журнал подставной — хост не нужен: `dsh_session_history` подменён так, что страница
отдаёт не больше `messages × 8` событий, новее засечки `cursor`, как настоящий
`session/page`. Задача кладётся вглубь журнала (сотни шагов инструментов между промптом
и ответом) — ровно та карточка, на которой `dsh_board_verify` спотыкался про хвост окна.
"""
import pytest

from dsh_ import dsh_session

T0 = 1_795_700_000          # секунды эпохи; журнал хоста хранит миллисекунды


def _user(seq, text, kind='user'):
    return {'type': 'user/message', 'seq': seq, 'time': T0 * 1000,
            'data': {'source': {'kind': kind}, 'content': [{'type': 'text', 'text': text}]}}


def _assistant(seq, text):
    return {'type': 'assistant/message', 'seq': seq, 'time': T0 * 1000,
            'data': {'message': {'content': [{'type': 'text', 'text': text}]}}}


def _splice(seq, text, target='next-turn'):
    return {'type': 'agent/inbox/spliced', 'seq': seq, 'time': T0 * 1000,
            'data': {'target': target, 'inserted': [{'content': [{'type': 'text', 'text': text}]}]}}


def _turn(seq, kind, turn, at):
    data = {'turn': turn}
    if kind == 'turn/end':
        data['reason'] = {'kind': 'completed'}
    return {'type': kind, 'seq': seq, 'time': at * 1000, 'data': data}


def _steps(first, count):
    """Шаги инструментов — шум между промптом и ответом, репликой диалога не считается."""
    return [{'type': 'tool/result', 'seq': first + i, 'time': T0 * 1000, 'data': {'name': 'bash'}}
            for i in range(count)]


@pytest.fixture
def journal(monkeypatch):
    """Журнал карточки: список событий задаёт тест, страница — окно по `cursor`."""
    box = _Box()

    def history(session, messages=dsh_session.DSH_SESSION_SPAN, cursor=None, *, base=None):
        up_to = max([event['seq'] for event in box.events] or [0]) + 1 if cursor is None else cursor
        rows = sorted((event for event in box.events if event['seq'] <= up_to),
                      key=lambda event: event['seq'])
        return rows[-messages * 8:]          # хост отдаёт события по возрастанию seq

    monkeypatch.setattr(dsh_session, 'dsh_session_history', history)
    box.events = []
    return box


class _Box:
    """Набор подмен фикстуры: сам журнал карточки."""


# ── пара «задача — ответ» ──

def test_pair_found_past_hundreds_of_tool_events(journal):
    """Задача на 15-й записи, ответ на 615-й — пара находится, а не «её нет в хвосте»."""
    journal.events = ([_user(15, 'Собери статистику по логам карточек')]
                      + _steps(16, 590) + [_assistant(615, 'Готово: 205 кругов, медиана 36 мин.')])
    pair = dsh_session.dsh_session_round_pair('card-1')
    assert pair['task'].startswith('Собери статистику')
    assert pair['answer'].startswith('Готово')
    assert pair['via'] == 'user'


def test_pair_of_last_round_not_the_first_one(journal):
    """Пара — последнего круга: промпт и ответ первого круга в паре не участвуют."""
    journal.events = [_user(2, 'первая задача'), _assistant(3, 'первый ответ'),
                      _user(4, 'вторая задача'), _assistant(5, 'второй ответ')]
    pair = dsh_session.dsh_session_round_pair('card-1')
    assert (pair['task'], pair['answer']) == ('вторая задача', 'второй ответ')


def test_pair_falls_back_to_goal_placed_by_board(journal):
    """Задачу положил бокс доски — реплики человека нет, годится вставка `next-turn`."""
    journal.events = [_splice(8, 'Разбери журнал waiter-ов')] + _steps(9, 30) + \
        [_assistant(60, 'Разобрал.')]
    pair = dsh_session.dsh_session_round_pair('card-1')
    assert pair['task'] == 'Разбери журнал waiter-ов' and pair['via'] == 'goal'


def test_service_splice_is_not_a_task(journal):
    """Служебные вставки (`next-step`) задачей не становятся — иначе проверят не то."""
    journal.events = [_splice(8, 'Фоновая задача кончилась', target='next-step'),
                      _assistant(9, 'Ответ без заданной задачи.')]

    pair = dsh_session.dsh_session_round_pair('card-1')
    assert pair['answer'].startswith('Ответ') and pair['task'] == '' and pair['via'] == ''


def test_dialog_pages_back_until_replies_are_enough(journal):
    """`dsh_session_dialog` доходит до промпта через страницы: хвост одних ответов — не ответ."""
    events, seq = [], 1
    for round_no in range(4):                            # четыре круга по 300 шагов
        events += [_user(seq, f'задача {round_no}'), _assistant(seq + 300, f'ответ {round_no}')]
        events += _steps(seq + 1, 299)
        seq += 400
    journal.events = events
    rows = dsh_session.dsh_session_dialog('card-1', 4)
    assert [row['role'] for row in rows] == ['user', 'assistant', 'user', 'assistant']
    assert [row['text'] for row in rows if row['role'] == 'user'] == ['задача 2', 'задача 3']


# ── границы кругов ──

def test_turn_log_counts_finished_and_running_rounds(journal):
    """Два готовых круга и один идущий: длительности есть, у идущего конца нет."""
    journal.events = [_turn(1, 'turn/start', 1, T0), _turn(40, 'turn/end', 1, T0 + 600),
                      _turn(41, 'turn/start', 2, T0 + 700), _turn(60, 'turn/end', 2, T0 + 1300),
                      _turn(61, 'turn/start', 3, T0 + 1400)]
    rows = dsh_session.dsh_session_turn_log('card-1', rounds=3)
    assert [(row['turn'], row['end'] and row['end'] - row['start']) for row in rows] == \
        [(1, 600), (2, 600), (3, None)]


def test_walk_back_stops_when_page_stops_moving(journal):
    """Хост отдал ту же страницу дважды — ходьба кончается, а не крутится до потолка."""
    journal.events = [_assistant(5, 'ответ')]
    pair = dsh_session.dsh_session_round_pair('card-1')
    assert pair['answer'] == 'ответ' and pair['task'] == ''

"""Тесты арифметики rabbitmq_stats на JSON management-API (без сети)."""
from rabbitmq_.rabbitmq_stats import rabbitmq_stats_row, rabbitmq_stats_sort

# снимок prod 2026-11-19: 55 не подтверждено, 54 вернулись при убийстве пода, 1 — в DLX
MAIN = {'name': 'check_payment_status', 'type': 'classic', 'messages': 0,
        'arguments': {'x-dead-letter-exchange': 'dl', 'x-dead-letter-routing-key': 'check_payment_status'},
        'message_stats': {'publish': 53749, 'get': 53896, 'deliver': 0, 'ack': 53841, 'redeliver': 54}}
DLQ = {'name': 'check_payment_status_dl', 'type': 'classic', 'messages': 1, 'messages_ready': 1,
       'arguments': {}, 'message_stats': {}}


def test_rabbitmq_stats_row_dead_lettered_estimate():
    """not_acked = get+deliver-ack, dead_est = not_acked-redeliver — совпадает с DLQ."""
    row = rabbitmq_stats_row(MAIN)
    assert (row['not_acked'], row['dead_est']) == (55, 1)
    assert row['dlx'] == 'dl -> check_payment_status'


def test_rabbitmq_stats_row_dlq_without_stats():
    """У DLQ нет message_stats (dead-letter не считается publish) — нули, без DLX-оценки."""
    row = rabbitmq_stats_row(DLQ)
    assert (row['messages'], row['publish'], row['dead_est'], row['dlq']) == (1, 0, 0, True)


def test_rabbitmq_stats_row_no_dlx_no_estimate():
    """Без DLX оценка ушедших в DLX не считается, даже если not_acked > 0."""
    row = rabbitmq_stats_row({**MAIN, 'arguments': {}})
    assert row['dead_est'] == 0 and row['not_acked'] == 55


def test_rabbitmq_stats_sort_dlq_with_messages_first():
    """DLQ с сообщениями сверху, затем очереди с сообщениями, затем пустые."""
    rows = [rabbitmq_stats_row(q) for q in (
        MAIN, {'name': 'busy', 'messages': 3}, DLQ, {'name': 'empty_dl', 'messages': 0})]
    assert [r['name'] for r in rabbitmq_stats_sort(rows)] == [
        'check_payment_status_dl', 'busy', 'check_payment_status', 'empty_dl']

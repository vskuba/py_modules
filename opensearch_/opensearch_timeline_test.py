"""Тесты ленты opensearch_timeline и разбора строки запроса opensearch_filters (без сети)."""
import pytest

from opensearch_.opensearch_ import opensearch_filters
from opensearch_.opensearch_timeline import opensearch_timeline_format, opensearch_timeline_rows

POD = 'cashier-payment-gateway-daemon-consumer.check-payment-statmxzhf'


def _hit(ts, message=None, level='INFO', text=None):
    """Хит в форме opensearch_hits: jsonPayload, либо textPayload stderr."""
    src = {'resource': {'labels': {'pod_name': POD}}, 'timestamp': ts}
    if text is not None:
        src['textPayload'] = text
    else:
        src['jsonPayload'] = {'logLevel': level, 'message': message}
    return {'ts': ts, '_source': src}


def test_opensearch_filters_lucene_string_becomes_query_string():
    """Не-JSON строка уходит в query_string, а не роняет разбор JSON."""
    parts = opensearch_filters('jsonPayload.message:*nack*')
    assert parts == [{'query_string': {'query': 'jsonPayload.message:*nack*'}}]


def test_opensearch_filters_all_and_json_kept():
    """all/* — match_all, JSON clause — как есть."""
    assert opensearch_filters('*') == [{'match_all': {}}]
    assert opensearch_filters('{"term": {"a": 1}}') == [{'term': {'a': 1}}]


def test_opensearch_filters_broken_json_fails_loudly():
    """Строка с `{`, но битая — ошибка, а не тихий query_string."""
    with pytest.raises(SystemExit):
        opensearch_filters('{"bad')


def test_opensearch_timeline_rows_hides_sql_keeps_rollback():
    """SQL-шум DEBUG спрятан, Rolling back — след ошибки — оставлен."""
    rows = opensearch_timeline_rows([
        _hit('2026-11-17T12:12:02.682000+00:00', 'Executing statement: UPDATE payment', 'DEBUG'),
        _hit('2026-11-17T12:12:02.683000+00:00', 'Beginning transaction', 'DEBUG'),
        _hit('2026-11-17T12:12:02.719000+00:00', 'Rolling back transaction', 'DEBUG'),
    ])
    assert [r['message'] for r in rows] == ['Rolling back transaction']


def test_opensearch_timeline_rows_sql_flag_shows_everything():
    """--sql возвращает DEBUG SQL."""
    rows = opensearch_timeline_rows([_hit('t1', 'Executing statement: SELECT 1', 'DEBUG')], sql=True)
    assert len(rows) == 1


def test_opensearch_timeline_rows_merges_duplicated_records():
    """Две записи с одним временем и текстом (разные insertId) — одна строка."""
    hit = _hit('2026-11-17T12:12:02.493897+00:00', 'Trace created')
    assert len(opensearch_timeline_rows([hit, dict(hit)])) == 1


def test_opensearch_timeline_rows_stderr_and_not_info():
    """stderr-трейс виден с уровнем '-', --not-info выкидывает INFO, но не stderr."""
    rows = opensearch_timeline_rows([
        _hit('t1', 'Start check payment', 'INFO'),
        _hit('t2', 'SafeCharge check status returned status error', 'NOTICE'),
        _hit('t3', text='  The EntityManager is closed.  '),
        _hit('t4', text='   '),
    ], not_info=True)
    assert [(r['level'], r['message']) for r in rows] == [
        ('NOTICE', 'SafeCharge check status returned status error'),
        ('-', 'The EntityManager is closed.')]


def test_opensearch_timeline_format_clock_and_pod_tail():
    """Строка: часы до миллисекунд, хвост пода, сообщение в одну строку."""
    row = opensearch_timeline_rows([_hit('2026-11-17T12:12:02.719000+00:00', 'a\nb', 'DEBUG')],
                                   sql=True)[0]
    assert opensearch_timeline_format(row) == '12:12:02.719  mxzhf  DEBUG    a b'

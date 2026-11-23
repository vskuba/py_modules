"""Тесты разбора алертов slack_alerts и refresh bot-токена (без сети)."""
import slack_.slack_alerts as alerts

FIRING_2 = {'ts': '1795013580.0', 'bot_id': 'B1', 'attachments': [{
    'title': '[FIRING:2] TooManyMessagesInDeadLetterQueueAppProd',
    'text': ('*Alert:* [`prod`] Dead letter queue worker_import_dl filling up\n'
             'Queue: `worker_import_dl`\nNum messages: `1`\n\n'
             '*Alert:* [`prod`] Dead letter queue worker_reports_dl filling up\n'
             'Queue: `worker_reports_dl`\nNum messages: `5`')}]}
FIRING_1 = {'ts': '1795004280.0', 'bot_id': 'B1', 'attachments': [{
    'title': '[FIRING:1] TooManyMessagesInDeadLetterQueueAppProd',
    'text': '*Alert:* x\nQueue: `worker_import_dl`'}]}
RESOLVED = {'ts': '1795100000.0', 'bot_id': 'B1', 'attachments': [{
    'title': '[RESOLVED] TooManyMessagesInDeadLetterQueueAppProd',
    'text': '*Alert:* x\nQueue: `worker_import_dl`'}]}
ELASTALERT = {'ts': '1795060000.0', 'subtype': 'bot_message', 'attachments': [{
    'title': '[prod] Import Worker Failed',
    'text': 'Worker `acme-agent` / <https://x|link>\ncustomer email: someone@example.com'}]}
HUMAN = {'ts': '1795000000.0', 'user': 'U1', 'text': 'глянь алерты, тут письмо a@b.c'}


def test_slack_alerts_events_split_firing_n_into_alerts():
    """[FIRING:2] с двумя блоками *Alert:* — два события с разными очередями."""
    events = alerts.slack_alerts_events([FIRING_2])
    assert [(e['state'], e['subject']) for e in events] == [
        ('FIRING', 'worker_import_dl'), ('FIRING', 'worker_reports_dl')]


def test_slack_alerts_events_elastalert_keeps_only_ticks_of_first_line():
    """У elastalert предмет — `…`-значения первой строки; тело (почта) не попадает."""
    (event,) = alerts.slack_alerts_events([ELASTALERT])
    assert (event['name'], event['subject']) == ('[prod] Import Worker Failed', 'acme-agent')
    assert 'example.com' not in str(event)


def test_slack_alerts_events_skip_human_messages():
    """Реплики людей не алерты — их текст в события не идёт."""
    assert alerts.slack_alerts_events([HUMAN]) == []


def test_slack_alerts_summary_first_last_repeats_resolved():
    """Группа (имя, очередь): первый/последний FIRING, повторы, RESOLVED, итоговое состояние."""
    rows = alerts.slack_alerts_summary(alerts.slack_alerts_events([FIRING_2, FIRING_1, RESOLVED]))
    dlq = next(r for r in rows if r['subject'] == 'worker_import_dl')
    assert (dlq['first'], dlq['fired'], dlq['resolved'], dlq['state']) == (
        1795004280.0, 2, 1, 'RESOLVED')


def test_slack_alerts_bot_token_live_token_no_refresh(tmp_path, monkeypatch):
    """Токен жив — сеть не трогается."""
    env = tmp_path / '.env'
    env.write_text('SLACK_BOT_TOKEN=live\nSLACK_BOT_TOKEN_EXPIRES_AT=2000\n')
    monkeypatch.setattr(alerts, 'slack_api_call', lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert alerts.slack_alerts_bot_token(str(env), now=1000) == 'live'


def test_slack_alerts_bot_token_refresh_writes_both_halves(tmp_path, monkeypatch):
    """Протух — refresh, новые access и одноразовый refresh записаны в файл."""
    env = tmp_path / '.env'
    env.write_text('OTHER=1\nSLACK_BOT_TOKEN=old\nSLACK_REFRESH_TOKEN=r1\n'
                   'SLACK_BOT_TOKEN_EXPIRES_AT=100\nSLACK_CLIENT_ID=c\nSLACK_CLIENT_SECRET=s\n')
    calls = []

    def fake(method, token='', params=None, **kw):
        calls.append((method, params['refresh_token']))
        return {'ok': True, 'access_token': 'new', 'refresh_token': 'r2', 'expires_in': 43200}, None

    monkeypatch.setattr(alerts, 'slack_api_call', fake)
    assert alerts.slack_alerts_bot_token(str(env), now=1000) == 'new'
    assert calls == [('oauth.v2.access', 'r1')]
    text = env.read_text()
    assert 'SLACK_BOT_TOKEN=new\n' in text and 'SLACK_REFRESH_TOKEN=r2\n' in text
    assert 'SLACK_BOT_TOKEN_EXPIRES_AT=44200\n' in text and text.startswith('OTHER=1\n')

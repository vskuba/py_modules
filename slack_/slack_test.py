"""Тесты разбора тела ответа slack_ (без сети)."""
import gzip

from slack_.slack_ import _slack_decode_body


def test_slack_decode_body_gzip_by_header_and_magic():
    """Заголовки просят gzip, urllib не распаковывает — тело распаковывается само."""
    raw = gzip.compress(b'{"ok": true}')
    assert _slack_decode_body(raw, 'gzip') == '{"ok": true}'
    assert _slack_decode_body(raw, '') == '{"ok": true}'


def test_slack_decode_body_plain_untouched():
    """Несжатое тело — как есть."""
    assert _slack_decode_body(b'{"ok": false}', '') == '{"ok": false}'

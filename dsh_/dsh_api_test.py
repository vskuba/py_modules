"""Тесты `dsh_api`: транспортный сбой — это `DshApiUnreachable`, а не падение клиента.

28.11: живой хост оборвал ответ `IncompleteRead(0 bytes read)` посреди снимка доски,
`wait` умер трейсбеком с кодом 1 — `IncompleteRead` не `OSError` и проскакивал сквозь
«хост не ответил». `DSH_HOME` — в `tmp_path`, сеть не поднимается.
"""
import base64
import http.client
import json

import pytest

from dsh_ import dsh_api, dsh_board


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Дом хоста с поддельным секретом: cookie подписывается, запрос строится."""
    secret = base64.urlsafe_b64encode(bytes(range(32))).decode('ascii').rstrip('=')
    (tmp_path / '.credentials.yaml').write_text(
        'records:\n  client-connection/browser-session:\n'
        f'    payload:\n      secret: {secret}\n')
    monkeypatch.setenv('DSH_HOME', str(tmp_path))
    return tmp_path


class _FlakyBody:
    """Ответ хоста: либо тело целиком, либо обрыв на `read()`."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _flaky_urlopen(monkeypatch, bodies):
    """`urlopen` из списка ответов; обрываются именно на `read()`, как на живом хосте."""
    calls = []
    queue = list(bodies)

    def fake(request, timeout=None):
        calls.append(request)
        return _FlakyBody(queue.pop(0))
    monkeypatch.setattr(dsh_api.urllib.request, 'urlopen', fake)
    return calls


def _pauses(monkeypatch):
    """Паузы `dsh_api_poll` — в список, вместо сна."""
    slept = []
    monkeypatch.setattr(dsh_api.time, 'sleep', lambda s: slept.append(s))
    return slept


def test_incomplete_read_is_unreachable(home, monkeypatch):
    """Обрыв тела — `DshApiUnreachable`, а не `IncompleteRead` наружу: у клиента одна граница «переспросить»."""
    _flaky_urlopen(monkeypatch, [http.client.IncompleteRead(b'', 0)])

    with pytest.raises(dsh_api.DshApiUnreachable):
        dsh_api.dsh_api_get('bell.state')


def test_connection_reset_is_unreachable(home, monkeypatch):
    """`RemoteDisconnected` (он и `ConnectionResetError`) — туда же."""
    _flaky_urlopen(monkeypatch, [http.client.RemoteDisconnected('')])

    with pytest.raises(dsh_api.DshApiUnreachable):
        dsh_api.dsh_api_get('bell.state')


def test_poll_backs_off_then_answers(home, monkeypatch, capsys):
    """Два сбоя, паузы 1 и 2 с, строка в stderr — на третий попытке ответ как есть."""
    pauses = _pauses(monkeypatch)
    left = {'fails': 2}

    def fetch():
        if left['fails']:
            left['fails'] -= 1
            raise dsh_api.DshApiUnreachable('хост не ответил: IncompleteRead(0 bytes read)')
        return 'ок'

    value = dsh_api.dsh_api_poll(fetch)

    assert value == 'ок' and pauses == [1.0, 2.0]
    err = capsys.readouterr().err
    assert 'хост не ответил, повтор через 1 с' in err
    assert 'хост не ответил, повтор через 2 с' in err


def test_poll_does_not_retry_method_refusal(monkeypatch):
    """Отказ метода — не сетевой сбой: переспрашивать бессмысленно, летит сразу."""
    monkeypatch.setattr(dsh_api.time, 'sleep', lambda s: pytest.fail('повторил отказ'))
    calls = []

    def fetch():
        calls.append(1)
        raise dsh_api.DshApiError('session/not-found: карточки нет')

    with pytest.raises(dsh_api.DshApiError):
        dsh_api.dsh_api_poll(fetch)
    assert len(calls) == 1


def test_board_state_survives_flaky_host(home, monkeypatch):
    """Снимок доски сквозь живой сбой: два обрыва на нуле, третья попытка отвечает."""
    pauses = _pauses(monkeypatch)
    ok = json.dumps({'goals': [], 'robots': []}).encode('utf-8')
    calls = _flaky_urlopen(monkeypatch, [http.client.IncompleteRead(b'', 0),
                                         http.client.IncompleteRead(b'', 0), ok])

    state = dsh_board.dsh_board_state()

    assert state == {'goals': [], 'robots': []}
    assert len(calls) == 3 and pauses == [1.0, 2.0]


def test_poll_gives_up_with_the_error(home, monkeypatch):
    """Все паузы вышли — наружу уходит последняя ошибка: опросный цикл продолжит сам."""
    _pauses(monkeypatch)
    bodies = [http.client.IncompleteRead(b'', 0)] * (len(dsh_api.DSH_API_POLL_BACKOFF) + 1)
    calls = _flaky_urlopen(monkeypatch, bodies)

    with pytest.raises(dsh_api.DshApiUnreachable):
        dsh_api.dsh_api_poll(lambda: dsh_api.dsh_api_get('bell.state'))
    assert len(calls) == len(dsh_api.DSH_API_POLL_BACKOFF) + 1

"""Клиент socket.io второго поколения (Engine.IO 3) поверх `websockets`, без браузера.

Чат многих сайтов ходит socket.io, и страница держит его ради нескольких событий:
списка диалогов, сообщений, отметок. Вкладка Chromium ради этого стоит сотни
мегабайт и десятки секунд, а сам протокол — текстовые кадры по вебсокету.
Устройство и грабли — `docs/socketio.md`. Зависимость — `websockets` (≥ 13: довод
`additional_headers`), подключается лениво.

    async with socketio_v2_open(url) as sock:
        await sock.emit('message', {'type': 'cl_ch_all', 'data': {...}})
        event, args = await sock.next(15)

⚠ Только второе поколение сервера (`EIO=3`, клиент `socket.io 2.x` у страницы):
у третьего и четвёртого другое рукопожатие и другой ping — кто пинг шлёт, решает
версия. Признак сайта второго поколения — `socket.io.2.*.js` в разметке.

⚠ Только вебсокет, без опроса (`transport=polling`): сервер, который апгрейда не
даёт (`"upgrades": []`), принимает вебсокет сразу — так и говорит страница.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl


# Engine.IO 3: типы пакетов — первой цифрой кадра.
SOCKETIO_V2_OPEN = '0'
SOCKETIO_V2_PING = '2'
SOCKETIO_V2_PONG = '3'
SOCKETIO_V2_MESSAGE = '4'

# Socket.IO 2 внутри пакета `4`: `0` — подключились к пространству, `2` — событие,
# `4` — отказ пространства (обычно авторизация).
SOCKETIO_V2_CONNECT = '0'
SOCKETIO_V2_EVENT = '2'
SOCKETIO_V2_ERROR = '4'

# Сколько ждём рукопожатия. Сервер отвечает `0{…}` и `40` за доли секунды; дольше
# десяти — значит не тот адрес или нас не пустили.
SOCKETIO_V2_OPEN_SEC = 10

# Предел одного кадра. Сервер чата отдаёт список сообщений одним кадром, и
# умолчание `websockets` в 1 МБ — потолок, о который рвётся длинная переписка.
SOCKETIO_V2_FRAME_MAX = 2 ** 24


class SocketIoV2Error(Exception):
    """Сокет не открылся, сервер отказал пространству или закрыл соединение."""


class SocketIoV2:
    """Открытое соединение: слать события и читать следующие.

    ⚠ В Engine.IO 3 пинг шлёт **клиент**, а сервер отвечает `3`. Пока
    соединение используется, `next` шлёт пинг сам в срок `pingInterval` — без
    этого сервер закрывает сокет через `pingInterval + pingTimeout` молча.
    """

    def __init__(self, ws, hello: dict):
        self.ws = ws
        self.hello = hello
        self.sid = str(hello.get('sid') or '')
        self._ping_every = max(1.0, float(hello.get('pingInterval') or 25000) / 1000)
        self._ping_at = asyncio.get_running_loop().time() + self._ping_every

    async def emit(self, event: str, *args) -> None:
        """Шлёт событие `event` с доводами — кадр `42["event", …]`."""
        await self.ws.send(SOCKETIO_V2_MESSAGE + SOCKETIO_V2_EVENT
                           + json.dumps([event, *args], ensure_ascii=False))

    async def next(self, timeout: float) -> tuple[str, list]:
        """Следующее событие сервера: `(имя, доводы)`.

        Raises:
            asyncio.TimeoutError: за `timeout` секунд событий не было.
            SocketIoV2Error: сервер закрыл соединение или отказал пространству.

        Пинги, понги и служебные пакеты проглатываются здесь: вызывающему они
        не нужны, а пропусти он пинг — сервер закроет сокет.
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + float(timeout)

        while True:
            now = loop.time()
            if now >= end:
                raise asyncio.TimeoutError()

            if now >= self._ping_at:
                await self.ws.send(SOCKETIO_V2_PING)
                self._ping_at = now + self._ping_every

            try:
                raw = await asyncio.wait_for(self.ws.recv(),
                                             min(end, self._ping_at) - now)
            except asyncio.TimeoutError:
                continue
            except _closed() as e:
                raise SocketIoV2Error(f'сервер закрыл соединение: {e}')

            event = _event(raw)
            if event is not None:
                return event

            if raw == SOCKETIO_V2_PING:
                await self.ws.send(SOCKETIO_V2_PONG)


@asynccontextmanager
async def socketio_v2_open(url: str, query: dict = None, headers: dict = None):
    """Открывает соединение socket.io 2 и ждёт подключения к пространству.

    Args:
        url: адрес сервера вместе с путём socket.io —
            `wss://example.com/chat/socket.io/`. `EIO` и `transport` дописываются.
        query: строка запроса рукопожатия — так обычно едет токен.
        headers: заголовки рукопожатия (`Origin` сервер чата часто проверяет).

    Yields:
        SocketIoV2: открытое соединение; закрывается на выходе.

    Raises:
        SocketIoV2Error: не открылся, не прислал рукопожатие или отказал (`44`).
    """
    import websockets

    address = _address(url, query)

    try:
        ws = await websockets.connect(address, additional_headers=headers or None,
                                      max_size=SOCKETIO_V2_FRAME_MAX,
                                      open_timeout=SOCKETIO_V2_OPEN_SEC)
    except Exception as e:
        raise SocketIoV2Error(f'сокет не открылся: {type(e).__name__}: {e}')

    try:
        hello = await _handshake(ws)
        yield SocketIoV2(ws, hello)
    finally:
        await ws.close()


async def _handshake(ws) -> dict:
    """Ждёт `0{…}` и `40`; отдаёт параметры рукопожатия."""
    hello = None
    try:
        loop = asyncio.get_running_loop()
        end = loop.time() + SOCKETIO_V2_OPEN_SEC
        while True:
            raw = await asyncio.wait_for(ws.recv(), max(0.01, end - loop.time()))
            if raw.startswith(SOCKETIO_V2_MESSAGE + SOCKETIO_V2_CONNECT):
                return hello or {}
            if raw.startswith(SOCKETIO_V2_MESSAGE + SOCKETIO_V2_ERROR):
                raise SocketIoV2Error(f'сервер отказал: {raw[2:][:200]}')
            if raw.startswith(SOCKETIO_V2_OPEN):
                hello = json.loads(raw[1:] or '{}')
    except asyncio.TimeoutError:
        raise SocketIoV2Error('сервер не прислал рукопожатие')
    except _closed() as e:
        raise SocketIoV2Error(f'сервер закрыл соединение при рукопожатии: {e}')


def _closed():
    """Класс исключения «соединение закрыто» — `websockets` подключается лениво."""
    from websockets.exceptions import ConnectionClosed

    return ConnectionClosed


def _event(raw) -> tuple[str, list] | None:
    """Кадр `42["имя", …]` — в `(имя, доводы)`; прочее — `None`."""
    head = SOCKETIO_V2_MESSAGE + SOCKETIO_V2_EVENT
    if not isinstance(raw, str) or not raw.startswith(head):
        return None

    # Кадр с подтверждением несёт номер между `42` и `[`: `421["…"]`.
    body = raw[len(head):].lstrip('0123456789')
    try:
        packet = json.loads(body)
    except ValueError:
        return None

    if not isinstance(packet, list) or not packet:
        return None

    return str(packet[0]), packet[1:]


def _address(url: str, query: dict | None) -> str:
    """Адрес рукопожатия: свой запрос плюс `EIO=3&transport=websocket`."""
    parts = urlsplit(url)
    pairs = dict(parse_qsl(parts.query))
    pairs.update({k: str(v) for k, v in (query or {}).items() if v is not None})
    pairs.update({'EIO': '3', 'transport': 'websocket'})

    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/',
                       urlencode(pairs), ''))

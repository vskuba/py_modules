"""
Минимальный WebSocket-клиент на сокетах: рукопожатие, кадры, сообщения.

Слой транспорта, ничего не знающий о протоколе поверх него (`adb_cdp` зовёт
его и разговаривает JSON-вызовами). Написан на stdlib сознательно: ядро
пакета тощее (`py_modules/requirements.txt`), а библиотека WebSocket за один
рукопожатно-кадровый кусок протокола не оправдана.

Что учтено, потому что на практике и ломалось:

- клиентские кадры обязаны быть маскированы, серверные — нет;
- ответы приходят фрагментами — сообщение докладывается из кадров до FIN;
- ping от сервера требует ответного pong, иначе умный сервер (DevTools)
  закрывает соединение как протухшее.
"""
import base64
import os
import socket
import struct

# Магия рукопожатия WebSocket (RFC 6455), константа протокола.
ADB_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def adb_ws_open(url: str, timeout: float = 5.0) -> socket.socket:
    """
    Открыть WebSocket-соединение (`ws://host:port/path`).

    Args:
        url: адрес цели — для DevTools цели его даёт `adb_cdp_pages`.
        timeout: секунды на рукопожатие и на операции по сокету.

    Returns:
        Активный сокет; дальше — `adb_ws_send` / `adb_ws_recv`.

    Raises:
        RuntimeError: рукопожатие отклонено или оборвано.
        OSError: соединение не устанавливается.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path + ('?' + parts.query if parts.query else '')
    sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    handshake = (f'GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\n'
                 f'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                 f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
    sock.sendall(handshake.encode('ascii'))
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError(f'рукопожатие WebSocket оборвано: {url}')
        head += chunk
    status = head.split(b'\r\n', 1)[0]
    if b' 101 ' not in status:
        raise RuntimeError(f'WebSocket рукопожатие отклонено: {status.decode(errors="replace")}')
    return sock


def adb_ws_send(sock: socket.socket, payload: str) -> None:
    """
    Отправить текстовое сообщение одним кадром.

    Args:
        sock: сокет из `adb_ws_open`.
        payload: текст сообщения; маскирование — забота функции.
    """
    _ws_frame(sock, 0x1, payload.encode('utf-8'))


def adb_ws_recv(sock: socket.socket) -> str:
    """
    Прочитать одно текстовое сообщение, докладывая фрагменты до FIN.

    Служебные кадры отрабатываются на месте: ping — ответный pong, close —
    ошибка. Бинарные фрагменты складываются молча: сервер DevTools отвечает
    текстом, а смешивать кадры в одно сообщение протоколу все равно можно.

    Args:
        sock: сокет из `adb_ws_open`.

    Returns:
        Текст сообщения.

    Raises:
        RuntimeError: сервер закрыл соединение, замаскировал кадр или связь
            оборвалась на середине сообщения.
    """
    message = b''
    while True:
        head = _recv_exact(sock, 2)
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack('!H', _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack('!Q', _recv_exact(sock, 8))[0]
        if head[1] & 0x80:
            raise RuntimeError('сервер замаскировал кадр — нарушение RFC 6455')
        payload = _recv_exact(sock, length) if length else b''
        if opcode == 0x8:
            raise RuntimeError('WebSocket закрыт сервером')
        if opcode == 0x9:
            _ws_frame(sock, 0xA, payload)  # pong на ping
            continue
        if opcode in (0x0, 0x1, 0x2):
            message += payload
        if fin:
            return message.decode('utf-8')


def _ws_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Собрать и отправить кадр: заголовок с длиной, маска, masked-полезная нагрузка."""
    header = bytearray([0x80 | opcode])
    if len(payload) < 126:
        header.append(len(payload) | 0x80)
    elif len(payload) < 65536:
        header.append(126 | 0x80)
        header += struct.pack('!H', len(payload))
    else:
        header.append(127 | 0x80)
        header += struct.pack('!Q', len(payload))
    mask = os.urandom(4)
    header += mask
    sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    """Прочитать ровно count байт; обрыв связи — ошибка, а не неполный кадр."""
    data = b''
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise RuntimeError('связь оборвана на середине кадра')
        data += chunk
    return data

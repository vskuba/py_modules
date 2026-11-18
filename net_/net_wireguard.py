"""
WireGuard для одного процесса: TCP-проброс через туннель без root и без VPN на всю машину.

Родился на базах Cashier dev/stage, куда пускают только из VPN. Конфиг VPN там —
`AllowedIPs = 0.0.0.0/0`: `wg-quick up` уводит в туннель **весь** трафик машины,
требует sudo и мешает человеку за той же машиной. Агенту нужен не VPN, а один
порт базы на время запроса.

`wireproxy` (userspace WireGuard на gVisor, https://github.com/pufferffish/wireproxy)
читает обычный конфиг WireGuard и открывает локальный порт, ведущий через туннель
к цели. Ни интерфейса, ни маршрутов, ни root: туннель видит только тот, кто
подключился к этому порту, и живёт, пока жив процесс.

## Грабли

* ⚠⚠ **Один ключ — один туннель в моменте.** Конфиг, скопированный у человека,
  несёт его приватный ключ. Подними `wireproxy` при включённом `wg0` с тем же
  ключом — сервер будет перескакивать между двумя endpoint-ами и рвать обоих.
  Поэтому `net_wireguard_up()` проверяется первым: системный VPN поднят — идём
  через него, свой туннель не нужен. Работать параллельно — только с отдельным
  пиром (своим ключом) от владельцев VPN.
* Конфиг — секрет: права 600, копия для `wireproxy` пишется во временный файл
  600 и удаляется по выходе. `/etc/wireguard` обычному пользователю закрыт —
  человек копирует конфиг сам (`sudo install -m 600 -o $USER …`).
* Порт начинает слушать **до** рукопожатия: первое соединение подождёт
  handshake (обычно < 1 с) — это не зависание.
* DNS цели резолвится внутри туннеля серверами из `DNS =` конфига.
* Бинарь: `WIREPROXY_BIN`, иначе `PATH`, иначе `~/go/bin/wireproxy`;
  сборка — `go install github.com/pufferffish/wireproxy/cmd/wireproxy@v1.0.9`.

## Что здесь есть

* `net_wireguard_up` — поднят ли системный интерфейс WireGuard (`wg0`), без sudo.
* `net_wireguard_bin` — путь к `wireproxy`.
* `net_wireguard_tcp` — контекст: локальный `(host, port)`, ведущий к цели через туннель.

CLI (держит проброс до Ctrl+C):

    python -m net_.net_wireguard ~/.config/wireguard/agent.conf db.dev-example.com:3306
"""
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from net_.net_port import net_port_wait

NET_WIREGUARD_START = 10.0


def net_wireguard_up(interface: str = 'wg0') -> bool:
    """Поднят ли системный интерфейс WireGuard — по `/sys/class/net`, без sudo."""
    return Path('/sys/class/net', interface).exists()


def net_wireguard_bin() -> str:
    """Путь к `wireproxy`.

    Raises:
        RuntimeError: бинаря нет — с командой сборки.
    """
    found = os.environ.get('WIREPROXY_BIN') or shutil.which('wireproxy') \
        or str(Path.home() / 'go/bin/wireproxy')
    if not Path(found).is_file():
        raise RuntimeError('нет wireproxy: go install '
                           'github.com/pufferffish/wireproxy/cmd/wireproxy@v1.0.9')
    return found


@contextlib.contextmanager
def net_wireguard_tcp(conf: str, host: str, port: int, timeout: float = NET_WIREGUARD_START):
    """Проброс `127.0.0.1:<свободный>` → `host:port` через туннель на время блока.

    Args:
        conf: конфиг WireGuard (`[Interface]` + `[Peer]`), права 600.
        host, port: цель по ту сторону туннеля.
        timeout: сколько ждать, пока `wireproxy` займёт локальный порт.

    Yields:
        `('127.0.0.1', локальный_порт)`.

    Raises:
        RuntimeError: конфига нет, `wireproxy` упал или не поднял порт.
    """
    conf_path = Path(conf).expanduser()
    if not conf_path.is_file():
        raise RuntimeError(f'нет конфига WireGuard: {conf_path}')
    local_port = _free_port()
    fd, proxy_conf = tempfile.mkstemp(prefix='wireproxy-', suffix='.conf')
    with os.fdopen(fd, 'w') as fh:
        fh.write(f'WGConfig = {conf_path}\n\n[TCPClientTunnel]\n'
                 f'BindAddress = 127.0.0.1:{local_port}\nTarget = {host}:{port}\n')
    proc = subprocess.Popen([net_wireguard_bin(), '-s', '-c', proxy_conf],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        if not net_port_wait(local_port, up=True, timeout=timeout, step=0.1):
            proc.kill()
            err = (proc.communicate(timeout=2)[1] or '').strip()[-300:]
            raise RuntimeError(f'wireproxy не поднял 127.0.0.1:{local_port}: {err or "без вывода"}')
        yield '127.0.0.1', local_port
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.unlink(proxy_conf)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


if __name__ == '__main__':
    if len(sys.argv) != 3 or ':' not in sys.argv[2]:
        sys.exit('использование: python -m net_.net_wireguard <конфиг> <хост>:<порт>')
    target_host, target_port = sys.argv[2].rsplit(':', 1)
    with net_wireguard_tcp(sys.argv[1], target_host, int(target_port)) as (lhost, lport):
        print(f'{lhost}:{lport} → {sys.argv[2]} (Ctrl+C — закрыть)', flush=True)
        try:
            signal.pause()
        except KeyboardInterrupt:
            pass

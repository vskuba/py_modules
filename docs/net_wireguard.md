# WireGuard для одного процесса — `net_/net_wireguard.py`

> TCP-проброс через WireGuard без root и без VPN на всю машину: `wireproxy`
> (userspace, gVisor) открывает `127.0.0.1:<порт>` → цель по ту сторону туннеля.
> Потребитель — `mysql_query --profile` с ключом `MYSQL_<ИМЯ>_WIREGUARD`.

| Функция | Контракт |
|---|---|
| `net_wireguard_up(interface='wg0') -> bool` | поднят ли системный WireGuard (по `/sys/class/net`, без sudo) |
| `net_wireguard_bin() -> str` | путь к `wireproxy` (`WIREPROXY_BIN` → `PATH` → `~/go/bin`) |
| `net_wireguard_tcp(conf, host, port)` | контекст: `('127.0.0.1', порт)` на время блока; процесс и временный конфиг убираются |

```bash
go install github.com/pufferffish/wireproxy/cmd/wireproxy@v1.0.9      # один раз
python -m net_.net_wireguard ~/.config/wireguard/agent.conf db.host:3306  # держать до Ctrl+C
```

## Грабли

- ⚠⚠ **Один ключ — один туннель в моменте.** Скопированный у человека конфиг несёт
  его ключ: `wireproxy` при поднятом `wg0` с тем же ключом — сервер прыгает между
  endpoint-ами и рвёт обоих. Поэтому сначала `net_wireguard_up()`. Параллельно —
  только с отдельным пиром.
- ⚠ Конфиг с `AllowedIPs = 0.0.0.0/0` через `wg-quick` уводит **весь** трафик машины —
  ещё одна причина не поднимать системный VPN ради одного порта.
- `/etc/wireguard` пользователю закрыт: копию делает человек —
  `sudo install -m 600 -o $USER -g $USER /etc/wireguard/wg0.conf ~/.config/wireguard/agent.conf`.
- Порт слушает до рукопожатия: первое соединение ждёт handshake (< 1 с).

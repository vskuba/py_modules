"""
Кто держит сокеты машины: живые TCP-соединения — хост и каждый контейнер, по именам.

Разбор «что-то висит на соединении / кто-то долбит адрес» руками стоит четырёх
шагов: `ss -tn`, потом угадывание pid'ов, потом обход `/proc/*/fd` или docker, и
только затем ответ «наше это или соседнее». `net_port` отвечает на «порт жив ли»
и сознательно без владельцев; здесь другой вопрос — **чей** сокет и **куда** он
тянется. Одна команда: таблица соединений с колонкой контейнера.

Хост видит только своё сетевое пространство: сокеты контейнера с своим netns
наружу не видны вовсе. Поэтому инструмент ходит и в хост (`ss`), и в каждый
поднятый контейнер (его `/proc/net/tcp*`: `ss` в рабочих образах не водится).

## Что здесь есть

* `net_sockets_list` — строки-соединения: состояние, адреса, процесс и контейнер.

⚠ Без прав колонка процесса у чужих сокетов пуста: `ss` показывает процессы
своего владельца. Контейнер при этом всё равно определяется — `docker` отвечает,
а цепочка родителей в `/proc` читается у любого pid.
"""
import argparse
import json
import re
import subprocess

# Состояния TCP: код из /proc/net/tcp к имени, которое печатает ss. Живым
# соединением считаем всё, кроме LISTEN; TIME_WAIT — уже не соединение, его
# видно с `all_states`.
NET_SOCKETS_STATE = {'01': 'ESTAB', '02': 'SYN-SENT', '03': 'SYN-RECV', '04': 'FIN-WAIT-1',
                     '05': 'FIN-WAIT-2', '06': 'TIME-WAIT', '08': 'CLOSE-WAIT',
                     '09': 'LAST-ACK', '0A': 'LISTEN', '0B': 'CLOSING'}
NET_SOCKETS_ALIVE = tuple(name for code, name in NET_SOCKETS_STATE.items()
                          if code not in ('0A', '06'))  # не LISTEN и не TIME-WAIT

# Строка `ss -tanp` с процессом и без: состояние, очереди, свой адрес, сосед,
# опционально `users:(("cmd",pid=N,fd=M))`.
NET_SOCKETS_ROW = re.compile(r'^(\S+)\s+\d+\s+\d+\s+(\S+)\s+(\S+)(?:\s+users:.*?pid=(\d+).*)?$')


def net_sockets_list(all_states: bool = False, grep: str = '',
                     containers: bool = True) -> list:
    """
    Живые TCP-соединения машины: хост и каждый поднятый контейнер.

    Args:
        all_states: включить LISTEN и TIME_WAIT — всё, что считает `ss -a`.
        grep: оставить только строки с этой подстрокой в любом поле.
        containers: обойти контейнеры (`docker exec`); False — только хост.

    Returns:
        Список `{'container', 'state', 'local', 'peer', 'pid', 'command'}` —
        `container` пуст у хостовых, `pid` и `command` пусты, когда процесса
        не видно (чужой владелец, внутренний сокет контейнера).

    ⚠ Порядок строк не гарантирован: хост и контейнеры опрашиваются подряд,
    а состояние соединения меняется между опросами. Это снимок, а не трекер.
    """
    inits = _container_inits()
    rows = []
    for state, local, peer, pid in _ss_host():
        rows.append({'container': _pid_container(pid, inits), 'state': state,
                     'local': local, 'peer': peer, 'pid': pid,
                     'command': _command(pid)})
    if containers:
        for name in _running():
            for state, local, peer in _proc_net(name):
                rows.append({'container': name, 'state': state, 'local': local,
                             'peer': peer, 'pid': 0, 'command': ''})

    rows = [r for r in rows if all_states or r['state'] in NET_SOCKETS_ALIVE]
    if grep:
        rows = [r for r in rows if grep in ' '.join(str(v) for v in r.values())]
    return sorted(rows, key=lambda r: (r['container'], r['state'], r['peer']))


# ── детали реализации ──

def _ss_host() -> list:
    """Хостовые сокеты: `(состояние, свой адрес, сосед, pid)`; без процесса — pid 0."""
    try:
        done = subprocess.run(['ss', '-tanp'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as err:
        raise RuntimeError(f'`ss` не запустился: {err}')
    rows = []
    for line in done.stdout.splitlines():
        match = NET_SOCKETS_ROW.match(line)
        if not match:                             # заголовок и недоразобранное
            continue
        state, local, peer, pid = match.groups()
        rows.append((state, local, peer, int(pid) if pid else 0))
    return rows


def _running() -> list:
    """Имена поднятых контейнеров всех стеков; docker недоступен — пусто."""
    try:
        done = subprocess.run(['docker', 'ps', '--format', '{{.Names}}'],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    return done.stdout.split() if done.returncode == 0 else []


def _proc_net(name: str) -> list:
    """Сокеты контейнера из его `/proc/net/tcp*`: `(состояние, свой адрес, сосед)`.

    Читается `cat`, а не `ss`: в рабочих образах iproute2 не водится, а proc
    есть всегда. Отказ контейнера (упал, exec недоступен) — пустой ответ:
    живой ответ машины важнее одного дохшего стека.
    """
    try:
        done = subprocess.run(['docker', 'exec', name, 'cat',
                               '/proc/net/tcp', '/proc/net/tcp6'],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[3] not in NET_SOCKETS_STATE:     # заголовок таблицы — не состояние
            continue
        rows.append((NET_SOCKETS_STATE[parts[3]], _hex_addr(parts[1]), _hex_addr(parts[2])))
    return rows


def _hex_addr(token: str) -> str:
    """Адрес из /proc: v4 — четыре байта наоборот; v6 — четыре слова наоборот.

    v4-отображённый v6 (`::ffff:...`, мост docker пишет именно так) сворачивается
    в обычный IPv4 — иначе половина таблицы в вёрстке из 33 знаков, и адрес моста
    не отличить от настоящего v6. Голый v6 берётся в скобки: в нём восемь групп
    через `:`, и без скобок конец адреса и порт не разлепить глазами.
    """
    hexa, _, port = token.rpartition(':')
    port = int(port, 16)
    if len(hexa) == 8:
        return f'{_v4(hexa)}:{port}'
    if len(hexa) == 32:
        groups = []
        for word in (hexa[i:i + 8] for i in range(0, 32, 8)):
            flipped = bytes.fromhex(word)[::-1]
            groups += [flipped[:2].hex(), flipped[2:].hex()]
        if groups[:5] == ['0000'] * 5 and groups[5] == 'ffff':
            return f'{_v4_net(groups[6] + groups[7])}:{port}'   # v6-обёртка v4 — это v4
        return f"[{':'.join(groups)}]:{port}"
    return f'{hexa}:{port}'


def _v4(hexa: str) -> str:
    """v4 из /proc/net/tcp: лежат четыре байта в порядке машины — разворачиваем."""
    return '.'.join(str(byte) for byte in bytes.fromhex(hexa)[::-1])


def _v4_net(hexa: str) -> str:
    """v4, вписанный в v6 (`::ffff:...`): те же байты, но по-сетевому, не наоборот."""
    return '.'.join(str(byte) for byte in bytes.fromhex(hexa))


def _pid_container(pid: int, inits: dict) -> str:
    """Имя контейнера, которому принадлежит процесс: вверх по родителям до init."""
    for _ in range(64):                           # глубина цепочки с запасом
        if not pid:
            return ''
        if pid in inits:
            return inits[pid]
        pid = _ppid(pid)
    return ''


def _container_inits() -> dict:
    """pid первых процессов поднятых контейнеров → имена (pid'ы хостовые)."""
    names = _running()
    if not names:
        return {}
    try:
        done = subprocess.run(['docker', 'inspect', '--format', '{{.State.Pid}} {{.Name}}',
                               *names], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in done.stdout.splitlines():
        pid, _, name = line.partition(' ')
        if pid.isdigit():
            found[int(pid)] = name.lstrip('/')
    return found


def _ppid(pid: int) -> int:
    """Родитель pid'а из /proc; не видно родителя — 0."""
    try:
        with open(f'/proc/{pid}/stat', 'rb') as handle:
            # Родитель — 4-е поле; первые два по позиции считать нельзя: comm
            # бывает с пробелом и скобками — делим после последней `)`.
            return int(handle.read().rsplit(b')', 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def _command(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/comm', 'r') as handle:
            return handle.read().strip()
    except OSError:
        return ''


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Кто держит сокеты машины: TCP-соединения хоста и контейнеров.')
    ap.add_argument('--all', action='store_true', help='включая LISTEN и TIME-WAIT')
    ap.add_argument('--grep', default='', help='фильтр подстрокой по любому полю')
    ap.add_argument('--host', action='store_true', help='не обходить контейнеры')
    ap.add_argument('--format', default='table', choices=['table', 'json'])
    ns = ap.parse_args()
    try:
        rows = net_sockets_list(all_states=ns.all, grep=ns.grep, containers=not ns.host)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

    if ns.format == 'json':
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(f"{'КОНТЕЙНЕР':<18} {'СОСТОЯНИЕ':<12} {'СВОЙ':<26} "
              f"{'СОСЕД':<26} ПРОЦЕСС")
        for r in rows:
            process = f'{r["command"]}({r["pid"]})' if r['pid'] else ''
            print(f"{r['container'] or 'хост':<18} {r['state']:<12} {r['local']:<26} "
                  f"{r['peer']:<26} {process}")
        tally = {}
        for r in rows:
            tally[r['state']] = tally.get(r['state'], 0) + 1
        summary = ', '.join(f'{state} — {n}' for state, n in sorted(tally.items()))
        print(f"(соединений: {len(rows)}; {summary})")

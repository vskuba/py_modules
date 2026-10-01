"""
Процессы машины без `ps`: лист и дерево из `/proc/<pid>/`, CPU-такты из `/proc/<pid>/stat`.

`ps aux` агенту негоден так же, как `free`: разбор колонок под каждую версию
procps — отдельная работа, а лист процессов читается прямо из `/proc/<pid>/`
без шелла и без зависимости от утилит. Здесь — «кто есть» и «кто что жрет по
процессорному времени», память — у `proc_mem`.
"""
import os

# ── публичный API модуля ──


def proc_ps_list(limit: int = 30, root: str = '/proc') -> tuple:
    """Лист процессов: pid, comm, cmdline — с верха списка (первые pid — старее).

    Args:
        limit: сколько процессов вернуть; 0 или меньше — все процессы.
        root: корень proc-файловой системы (в контейнере — другой).

    Returns:
        ([{'pid': int, 'comm': str, 'cmdline': str}], None) либо (None, 'ошибка').
    """
    rows = []
    try:
        pids = sorted((d for d in os.listdir(root) if d.isdigit()), key=int)
    except OSError as err:
        return None, f'лист процессов не снят ({root}): {err}'
    for pid in pids:
        comm, cmdline = _proc_ps_read_cmdline(root, pid)
        if comm is None:
            continue
        rows.append({'pid': int(pid), 'comm': comm, 'cmdline': cmdline})
        if 0 < limit <= len(rows):
            break
    return rows, None


def proc_ps_find(pattern: str, root: str = '/proc') -> tuple:
    """Найти процессы по подстроке в comm/cmdline: pgrep-style без procps.

    Args:
        pattern: подстрока (регистр не трогается); ищет по comm и cmdline разом.
        root: корень proc-файловой системы.

    Returns:
        ([{'pid', 'comm', 'cmdline'}], None) либо (None, 'ошибка').
    """
    rows, err = proc_ps_list(limit=0, root=root)
    if err:
        return None, err
    needle = pattern.lower()
    return ([row for row in rows if needle in row['comm'].lower()
             or needle in row['cmdline'].lower()], None)


def proc_ps_stat(pid: int, root: str = '/proc') -> tuple:
    """Процесс-такты и резидент процесса: utime/stime из stat, VmRSS из status.

    Args:
        pid: идентификатор процесса.
        root: корень proc-файловой системы.

    Returns:
        ({'utime_jiffies', 'stime_jiffies', 'rss_kb', 'threads', 'state', 'uid'}, None)
        либо (None, 'ошибка').
    """
    info = {}
    try:
        stat = open(f'{root}/{pid}/stat', 'r', encoding='utf-8', errors='ignore').read()
    except OSError as err:
        return None, f'процесс {pid} не читается: {err}'
    body = stat[stat.rfind(')') + 2:]
    fields = body.split()
    if len(fields) > 20:
        info['utime_jiffies'] = int(fields[11])
        info['stime_jiffies'] = int(fields[12])
        info['state'] = fields[0]
        info['threads'] = int(fields[17]) if len(fields) > 17 else None
    try:
        for line in open(f'{root}/{pid}/status', 'r', encoding='utf-8'):
            if line.startswith(('VmRSS', 'Uid')):
                if line.startswith('VmRSS'):
                    info['rss_kb'] = int(line.split()[1])
                else:
                    info['uid'] = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return info, None


def proc_ps_tree(pid: int, root: str = '/proc') -> tuple:
    """Дерево потомков pid: кто породился от него — по PPID из `/proc/<pid>/stat`.

    Args:
        pid: идентификатор родителя.
        root: корень proc-файловой системы.

    Returns:
        ([{'pid', 'comm', 'cmdline'}], None) либо (None, 'ошибка').
    """
    rows, err = proc_ps_list(limit=0, root=root)
    if err:
        return None, err
    return ([row for row in rows if _proc_ps_ppid(root, row['pid']) == pid], None)


def proc_ps_port_owners(port: int, root: str = '/proc') -> tuple:
    """Кто держит TCP-порт: inode сокета из `/proc/net/tcp` → владелец из fd/.

    Args:
        port: номер порта.
        root: корень proc-файловой системы.

    Returns:
        ([{'pid', 'comm', 'cmdline'}], None) либо (None, 'ошибка').
    """
    inodes, err = _proc_ps_port_inodes(root, port)
    if err:
        return None, err
    if not inodes:
        return [], None
    owners = []
    for pid in sorted((d for d in os.listdir(root) if d.isdigit()), key=int):
        fd_dir = os.path.join(root, pid, 'fd')
        try:
            for fd in os.listdir(fd_dir):
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue  # fd мог сдохнуть между listdir и readlink — процесс просто пропускаем
                if link.startswith('socket:[') and link[8:-1] in inodes:
                    comm, cmdline = _proc_ps_read_cmdline(root, pid)
                    owners.append({'pid': int(pid), 'comm': comm, 'cmdline': cmdline})
                    break
        except OSError:
            continue
    return owners, None


# ── детали реализации ──


def _proc_ps_read_cmdline(root: str, pid: str) -> tuple:
    """Comm и cmdline процесса; None, comm — процесс сдохл между listdir и чтением."""
    try:
        comm = open(f'{root}/{pid}/comm', 'r', encoding='utf-8').read().strip()
        raw = open(f'{root}/{pid}/cmdline', 'rb').read()
        return comm, raw.replace(b'\x00', b' ').decode('utf-8', 'ignore').strip()
    except OSError:
        return None, ''


def _proc_ps_ppid(root: str, pid: int) -> int:
    """PPID процесса из /proc/<pid>/stat (поле 4, имя в скобках ломает split)."""
    try:
        stat = open(f'{root}/{pid}/stat', 'r', encoding='utf-8', errors='ignore').read()
        return int(stat[stat.rfind(')') + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return -1


def _proc_ps_port_inodes(root: str, port: int) -> tuple:
    """Inode'ы сокетов, слушающих port: разбор hex-портов из /proc/net/tcp{,6}."""
    inodes = set()
    hex_port = f'{port:04X}'
    found_any = False
    for proto in ('tcp', 'tcp6', 'udp', 'udp6'):
        try:
            lines = open(f'{root}/net/{proto}', 'r', encoding='utf-8').readlines()[1:]
        except OSError:
            continue
        found_any = True
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[1].endswith(':' + hex_port) and \
                    (proto.startswith('udp') or fields[3] == '0A'):
                inodes.add(fields[9])
    if not found_any:
        return None, f'{root}/net/tcp не читается: LISTEN-сокеты не сысканы'
    return inodes, None


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('list', 'find', 'stat', 'tree', 'ports'),
                    help='что показать из /proc')
    _P.add_argument('--pattern', default='', help='подстрока comm/cmdline для find')
    _P.add_argument('--pid', type=int, default=None, help='pid для stat/tree')
    _P.add_argument('--port', type=int, default=None, help='порт для ports')
    _P.add_argument('--limit', type=int, default=30, help='сколько строк печатать')
    _P.add_argument('--root', default='/proc', help='корень proc-файловой системы')
    _C = _P.parse_args()
    if _C.cmd == 'list':
        _rows, _err = proc_ps_list(_C.limit, _C.root)
    elif _C.cmd == 'find':
        if not _C.pattern:
            print('ошибка: для find нужен --pattern', file=sys.stderr)
            raise SystemExit(1)
        _rows, _err = proc_ps_find(_C.pattern, _C.root)
    elif _C.cmd == 'tree':
        if _C.pid is None:
            print('ошибка: для tree нужен --pid', file=sys.stderr)
            raise SystemExit(1)
        _rows, _err = proc_ps_tree(_C.pid, _C.root)
    elif _C.cmd == 'stat':
        if _C.pid is None:
            print('ошибка: для stat нужен --pid', file=sys.stderr)
            raise SystemExit(1)
        _row, _err = proc_ps_stat(_C.pid, _C.root)
        if _row is None:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        print(f"pid={_C.pid} " + ' '.join(f'{k}={v}' for k, v in _row.items()))
        raise SystemExit(0)
    else:
        if _C.port is None:
            print('ошибка: для ports нужен --port', file=sys.stderr)
            raise SystemExit(1)
        _rows, _err = proc_ps_port_owners(_C.port, _C.root)
    if _rows is None:
        print(f'ошибка: {_err}', file=sys.stderr)
        raise SystemExit(1)
    for _r in (_rows or [])[:_C.limit]:
        if _C.cmd in ('stat', 'tree'):
            print(f"pid={_r['pid']} {str(_r.get('comm', _r.get('cmdline', '')) or '')[:56]}")
        elif _C.cmd == 'ports':
            print(f"pid={_r['pid']} {_r['comm']}: {_r['cmdline'][:72]}")
        else:
            print(f"{_r['pid']:>8} {_r['comm'][:24]:<24} {_r['cmdline'][:64]}")
    raise SystemExit(0)

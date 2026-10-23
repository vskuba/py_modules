"""
Раздача каталога HTTP с точной остановкой: подъём, повторный подъём, гашение.

Браузеру (headless-приёмка, MCP-сессия) `file://` часто закрыт, и относительные
ссылки оживают только через HTTP. Ручной `python -m http.server &` — лотерея в
три стороны: процесс сиротеет при гибели шелла, порт остаётся занятым до следующей
сессии, а `pkill -f http.server` снимает и самого зовущего — шаблон живёт в его
собственной cmdline (проверено дважды: обёртка bash умирала по SIGTERM раньше
сервера). Здесь подъём detachment-ом с файлом состояния в `/tmp`, а гашение — по
держателю порта через `proc_ps`, с проверкой, что на порту **наш** раздат, а не
чужая служба, которой этот порт достался.

## Что здесь есть

* `net_serve_up` — поднять (или заиметь живой) раздат каталога, вернуть URL и pid;
* `net_serve_down` — погасить ровно тот раздат, что поднимали;
* `net_serve_status` — жив ли и по какому адресу.

⚠ Раздат — только петлевой адрес (127.0.0.1): наружу каталог не открывается.
⚠ Для одного процесса-корма есть `web_shot_serve` (контекст-менеджер в нити);
он живёт, пока жив вызывающий, — для браузера в **другом** процессе не годится.
"""
import argparse
import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import time

from net_.net_port import net_port_free, net_port_wait
from proc_.proc_ps import proc_ps_kill

# Файлы состояния раздатчиков: «ключ каталога → {pid, port}» и лог ребёнка.
NET_SERVE_STATE = '/tmp/net_serve-%s.json'
NET_SERVE_LOG = '/tmp/net_serve-%s.log'

# Сколько ждём ребёнка после старта: bind почти мгновенный, минуты не нужно.
NET_SERVE_BOOT = 10.0

# Подпись в cmdline ребёнка — по ней гашение отличает свой раздат от чужой
# службы, которой достался тот же порт.
NET_SERVE_MARK = 'net_.net_serve'


def _net_serve_key(directory: str) -> str:
    """Ключ состояния по абсолютному пути каталога."""
    return hashlib.sha1(os.path.abspath(directory).encode()).hexdigest()[:12]


def _net_serve_state_read(directory: str) -> dict:
    """Состояние раздатчика каталога; {} если файла нет или он битый."""
    try:
        with open(NET_SERVE_STATE % _net_serve_key(directory), encoding='utf-8') as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) and 'port' in state else {}
    except (OSError, ValueError):
        return {}


def _net_serve_alive(pid: int) -> bool:
    """Жив ли pid (сигнал 0 ничего не посылает, но спрашивает)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def net_serve_up(directory: str, port: int = 0) -> dict:
    """Поднять HTTP-раздат каталога на 127.0.0.1; повторный вызов возвращает живой.

    Ребёнок живёт отдельным процессом (`start_new_session`), переживает гибель
    шелла и не сиротеет бесследно: pid и порт записаны в файл состояния, гасится
    `net_serve_down`. Тот же каталог второй раз — тот же раздат, без второго
    процесса.

    Args:
        directory: что раздавать.
        port: фиксированный порт; 0 — ядро выдаст свободный (конкуренты за
            один порт не дерутся).

    Returns:
        {'ok': bool, 'url': 'http://127.0.0.1:порт', 'port': int, 'pid': int,
         'reused': был ли поднят раньше (bool) или 'ошибка словами'}
    """
    if not os.path.isdir(directory):
        return {'ok': False, 'error': f'каталога нет: {directory}'}
    key = _net_serve_key(directory)
    state = _net_serve_state_read(directory)
    if state.get('port') and _net_serve_alive(state.get('pid', -1)) \
            and not net_port_free(state['port']):
        if port and port != state['port']:
            return {'ok': False,
                    'error': f'каталог уже раздаётся на :{state["port"]}, '
                             f'просят :{port}; сначала net_serve_down'}
        return {'ok': True, 'url': f'http://127.0.0.1:{state["port"]}',
                'port': state['port'], 'pid': state['pid'], 'reused': True}
    if state.get('port') and not net_port_free(state['port']):
        return {'ok': False, 'error': f'порт :{state["port"]} занят не нами — '
                                      'разбирайтесь, состояние снимаю',
                'state': state}
    if port and not net_port_free(port):
        return {'ok': False, 'error': f'порт :{port} уже занят'}
    if state:
        try:
            os.unlink(NET_SERVE_STATE % key)
        except OSError:
            pass
    args = [sys.executable, '-m', NET_SERVE_MARK, 'serve', directory,
            '--port', str(port), '--state', NET_SERVE_STATE % key]
    with open(NET_SERVE_LOG % key, 'wb') as log:
        proc = subprocess.Popen(args, stdout=log, stderr=log,
                                start_new_session=True)
    deadline = time.monotonic() + NET_SERVE_BOOT
    while time.monotonic() < deadline:
        state = _net_serve_state_read(directory)
        if state.get('port'):
            if net_port_wait(state['port'], timeout=2):
                return {'ok': True, 'url': f'http://127.0.0.1:{state["port"]}',
                        'port': state['port'], 'pid': state['pid'], 'reused': False}
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    tail = ''
    try:
        tail = open(NET_SERVE_LOG % key, 'rb').read()[-300:].decode('utf-8', 'replace')
    except OSError:
        pass
    return {'ok': False, 'error': f'раздат не поднялся: {tail.strip() or "тишина"}'}


def net_serve_down(directory: str = '', port: int = 0) -> dict:
    """Погасить раздат — по каталогу или порту; чужой порт не трогает.

    Мишень — держатели порта (`proc_ps_port_owners`), из которых убиты только
    те, в ком cmdline помечена `net_.net_serve`: если на порту стоит чужая
    служба, это ошибка, а не мишень. Файл состояния снимается.

    Args:
        directory: каталог, чей раздат гасим (по нему ищем состояние).
        port: номер порта; без него — port из состояния каталога.

    Returns:
        {'ok': bool, 'killed': [pid], 'error': словами (пусто при удаче)}
    """
    if directory and not port:
        port = _net_serve_state_read(directory).get('port', 0)
    if not port:
        return {'ok': False, 'killed': [], 'error': 'неизвестно, какой порт гасить: '
                                                    'дай directory или port'}
    res = proc_ps_kill(port=port)  # сухой прогон: сначала разведка мишеней
    ours = [t for t in res['targets'] if NET_SERVE_MARK in t['cmdline']]
    if not ours:
        return {'ok': False, 'killed': [],
                'error': f'на :{port} нет нашего раздата '
                         f'(чужих процессов не трогаю)'}
    killed = []
    for target in ours:
        try:
            os.kill(target['pid'], 15)
            killed.append(target['pid'])
        except OSError:
            continue  # мог умереть между разведкой и сигналом
    net_port_wait(port, up=False, timeout=5)
    if directory:
        try:
            os.unlink(NET_SERVE_STATE % _net_serve_key(directory))
        except OSError:
            pass
    return {'ok': bool(killed), 'killed': killed,
            'error': '' if killed else 'процесс не умер'}


def net_serve_status(directory: str = '', port: int = 0) -> dict:
    """Жив ли раздат и по какому адресу; только спрашивает, ничего не меняет.

    Args:
        directory: каталог — по нему ищем состояние.
        port: номер порта вместо состояния каталога.

    Returns:
        {'ok': bool, 'url': …, 'port': …, 'alive': bool}
    """
    if directory and not port:
        port = _net_serve_state_read(directory).get('port', 0)
    if not port:
        return {'ok': False, 'url': '', 'port': 0, 'alive': False}
    return {'ok': True, 'url': f'http://127.0.0.1:{port}', 'port': port,
            'alive': not net_port_free(port)}


def _net_serve_child(directory: str, port: int, state_path: str) -> None:
    """Тело ребёнка-раздатчика: bind, запись состояния, serve_forever до SIGTERM."""
    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = lambda *a, **kw: _Quiet(*a, directory=directory, **kw)  # noqa: E731
    with socketserver.ThreadingTCPServer(('127.0.0.1', port), handler) as srv:
        srv.daemon_threads = True
        tmp = state_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'pid': os.getpid(), 'port': srv.server_address[1],
                       'directory': os.path.abspath(directory)}, fh)
        os.replace(tmp, state_path)
        srv.serve_forever()


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('up', 'down', 'status', 'serve'))
    _P.add_argument('directory', nargs='?', default='', help='что раздавать')
    _P.add_argument('--port', type=int, default=0, help='порт; 0 — свободный')
    _P.add_argument('--state', default='', help='для serve: файл состояния')
    _C = _P.parse_args()
    if _C.cmd == 'up':
        if not _C.directory:
            raise SystemExit('ошибка: для up нужен каталог')
        _r = net_serve_up(_C.directory, _C.port)
    elif _C.cmd == 'down':
        _r = net_serve_down(_C.directory, _C.port)
    elif _C.cmd == 'status':
        _r = net_serve_status(_C.directory, _C.port)
    else:
        if not _C.directory or not _C.state:
            raise SystemExit('ошибка: для serve нужны каталог и --state')
        try:
            _net_serve_child(_C.directory, _C.port, _C.state)
        except KeyboardInterrupt:
            pass
        raise SystemExit(0)
    print(json.dumps(_r, ensure_ascii=False))
    raise SystemExit(0 if _r.get('ok') else 1)

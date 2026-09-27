"""Порт как предмет: свободен ли сейчас и дождаться, пока займёт или освободит.

Поднять сервис и сказать «готов» — разные утверждения, и второе всегда про
порт: процесс ещё жив, а уже мертв; процесс ещё грузит weights, а человек
спрашивает, почему интерфейс не открылся. `nc -z` и цикл с grep глазами для
агента негодны одинаково, а решение — сокет-попытка на полсекунды.

Пара в одном модуле, потому что вопрос один с двух сторон: `free` отвечает
сейчас, `wait` отвечает «когда будет» — и ждёт ровно того же признака, за тем
обратным знаком. Владельца порта тут нет нарочно: `ss -tlnp` требует прав и
вывода, который нужно снова парсить, а решение «порт занят — это кто»
обычно принимает человек, а не код.
"""
import socket
import time

# Одна попыка соединения: порт, принимающий на себя, отвечает с петли мгновенно;
# секунда с запасом, и столько же — потолок честного отказа «подключился».
NET_PORT_PROBE = 1.0

# Между попытками ждать: слишком часто — шуметь, слишком редко — терять
# секунды там, где сервис поднялся в щель между опросами.
NET_PORT_STEP = 0.25


def net_port_free(port: int, *, host: str = '127.0.0.1',
                  timeout: float = NET_PORT_PROBE) -> bool:
    """Свободен ли порт прямо сейчас: подключиться не к кому — значит свободен.

    Args:
        port: номер TCP-порта.
        host: откуда смотреть; по умолчанию петля — та сторона, где живут
            сервисы этой машины.
        timeout: потолок одной попытки соединения.

    Returns:
        True — никто не принял; False — порт занят (или кто-то принимает).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return False
    except OSError:
        return True


def net_port_wait(port: int, *, up: bool = True, host: str = '127.0.0.1',
                  timeout: float = 30.0, step: float = NET_PORT_STEP) -> bool:
    """Дождаться, когда порт займётся (`up=True`) или освободится (`up=False`).

    Возвращает **признак**, а не исключение: не дождаться — обычный ответ
    сторожа, а не авария вызывающего.

    Args:
        port: номер TCP-порта.
        up: чего ждать — чтобы стал занят (True) или освободился (False).
        host: откуда смотреть, петля по умолчанию.
        timeout: сколько секунд ждать всего.
        step: пауза между попытками.

    Returns:
        True — дождались, False — истёк срок, а порт не такой, как нужно.

    ⚠ `up` значит «кто-то принимает TCP», а не «сервис здоров»: процесс,
    висящий на полпути к готовности, порт уже держит, и `up` наступит раньше,
    чем тот сможет ответить по делу.
    """
    deadline = time.monotonic() + timeout
    while True:
        free = net_port_free(port, host=host)
        if free != up:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(step, max(0.0, deadline - time.monotonic())))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='порт: free — свободен ли сейчас, wait — дождаться; '
                    'код возврата 0 — правда, 1 — нет.')
    ap.add_argument('verb', choices=('free', 'wait'), help='что спросить')
    ap.add_argument('port', type=int, help='номер TCP-порта')
    ap.add_argument('--host', default='127.0.0.1', help='откуда смотреть')
    ap.add_argument('--down', action='store_true',
                    help='wait: ждать освобождения, а не занятия')
    ap.add_argument('--timeout', type=float, default=None,
                    help='free: попыка; wait: всего секунд (по умолчанию 30)')
    ap.add_argument('--step', type=float, default=NET_PORT_STEP,
                    help='wait: пауза между попытками')
    ns = ap.parse_args()
    if ns.verb == 'free':
        free = net_port_free(ns.port, host=ns.host,
                             timeout=ns.timeout or NET_PORT_PROBE)
        print('свободен' if free else 'занят')
        raise SystemExit(0 if free else 1)
    up = not ns.down
    ok = net_port_wait(ns.port, up=up, host=ns.host, step=ns.step,
                       timeout=ns.timeout if ns.timeout is not None else 30.0)
    print('дождались' if ok else 'не дождались')
    raise SystemExit(0 if ok else 1)

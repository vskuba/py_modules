"""
Статистика очередей vhost облачной консоли: счётчики доставок, DLX, «получено, но не подтверждено».

Родилась на разборе DLQ кассира (2026-11). Вопрос «как сообщение попало в
`_dl`» решился не просмотром сообщения (`/get_messages` LB консоли режет, см.
`rabbitmq_peek`), а арифметикой `message_stats` основной очереди: `get` 53896
против `ack` 53841 при `redeliver` 54 — из 55 неподтверждённых 54 вернулись в
очередь при убийстве пода на деплое, и ровно одно отклонено в DLX (в DLQ лежит 1).
`rabbitmq_fleet` таких полей не показывает — он про обзор и чистку, а здесь про
разбор одной очереди.

## Поля строки

* `type`, `dlx` (`x-dead-letter-exchange` → routing key), `policy`;
* `messages` / `ready` / `unacked`, `consumers`;
* `publish`, `get`, `deliver`, `ack`, `redeliver` — накопительные счётчики
  `message_stats` **с последнего рестарта ноды** (не за всё время);
* `not_acked` = `get + deliver − ack` — сколько выданных сообщений не
  подтверждено: отклонено (nack/reject) или возвращено при обрыве канала;
* `dead_est` = `not_acked − redeliver` (для очередей с DLX) — оценка, сколько
  ушло в DLX: возвращённые при обрыве потом доставляются повторно и попадают
  в `redeliver`, отклонённые — нет. Сверяй с числом сообщений в `_dl`.

⚠ **Пустая `message_stats` у DLQ ничего не значит.** Сообщение, попавшее туда
dead-letter'ом, не считается `publish` в целевой очереди: `_dl` с пятью свежими
сообщениями показывает `pub=0`. Вывод «stats пусты — сообщение старое, до
рестарта» неверен (проверено на `crypto_callback_processing_dl`, 2026-11).
Счётчики живут на основной очереди — смотри `dead_est` там.

⚠ `consumers=0` у очереди со swarrot — норма: консьюмер забирает сообщения
`basic.get` (растёт `get`, а не `deliver`) и подписчиком не числится.

Конфиг доступов и порт — как у `rabbitmq_fleet` (`~/.config/rabbitmq_mgmt.json`).

CLI:

    # все очереди vhost: DLQ с сообщениями сверху
    python3 -m rabbitmq_.rabbitmq_stats --env prod --vhost cashier_payment_gateway
    # пара очередей
    python3 -m rabbitmq_.rabbitmq_stats --env prod --vhost cashier_payment_gateway \\
        --queue check_payment_status --queue check_payment_status_dl

Только чтение: ничего не публикует, не забирает и не удаляет.
"""
import argparse
import urllib.parse

from rabbitmq_.rabbitmq_ import rabbitmq_mgmt_get
from rabbitmq_.rabbitmq_fleet import rabbitmq_fleet_conf

RABBITMQ_STATS_DLQ_SUFFIX = '_dl'
RABBITMQ_STATS_COUNTERS = ('publish', 'get', 'deliver', 'ack', 'redeliver')


def rabbitmq_stats_queues(env: str, vhost: str, queues=(), conf: dict | None = None) -> list:
    """Строки статистики очередей vhost (см. `rabbitmq_stats_row`).

    Args:
        env: среда из конфига (`prod`/`stage`/`dev`).
        vhost: виртуальный хост.
        queues: имена очередей; пусто — все очереди vhost.
        conf: конфиг сред; None — `rabbitmq_fleet_conf()`.

    Returns:
        строки, отсортированные: DLQ с сообщениями, прочие с сообщениями, остальные.
    """
    conf = conf or rabbitmq_fleet_conf()
    if env not in conf:
        raise RuntimeError(f'среды {env!r} нет в конфиге; есть: {", ".join(sorted(conf))}')
    one = conf[env]
    base = f'queues/{urllib.parse.quote(vhost, safe="")}'
    if queues:
        raw = [_get(one, f'{base}/{urllib.parse.quote(q, safe="")}') for q in queues]
    else:
        raw = _get(one, base) or []
    return rabbitmq_stats_sort([rabbitmq_stats_row(q) for q in raw])


def rabbitmq_stats_row(queue: dict) -> dict:
    """JSON очереди management-API → плоская строка статистики (без сети)."""
    args = queue.get('arguments') or {}
    stats = queue.get('message_stats') or {}
    counters = {name: int(stats.get(name) or 0) for name in RABBITMQ_STATS_COUNTERS}
    messages = int(queue.get('messages') or 0)
    not_acked = counters['get'] + counters['deliver'] - counters['ack']
    dlx = args.get('x-dead-letter-exchange')
    return {
        'name': queue.get('name', ''),
        'type': queue.get('type', ''),
        'dlx': f"{dlx} -> {args.get('x-dead-letter-routing-key', '')}" if dlx else '',
        'policy': queue.get('policy') or '',
        'messages': messages,
        'ready': int(queue.get('messages_ready') or 0),
        'unacked': int(queue.get('messages_unacknowledged') or 0),
        'consumers': int(queue.get('consumers') or 0),
        **counters,
        'not_acked': not_acked,
        'dead_est': max(not_acked - counters['redeliver'], 0) if dlx else 0,
        'dlq': queue.get('name', '').endswith(RABBITMQ_STATS_DLQ_SUFFIX),
    }


def rabbitmq_stats_sort(rows: list) -> list:
    """DLQ с сообщениями → прочие с сообщениями → остальные; внутри по имени."""
    return sorted(rows, key=lambda r: (not (r['dlq'] and r['messages']), not r['messages'],
                                       r['name']))


def rabbitmq_stats_format(row: dict) -> str:
    """Строка таблицы: имя, тип, счётчики, not_acked, оценка ушедших в DLX."""
    marks = []
    if row['dlx']:
        marks.append(f"dlx {row['dlx']}")
    if row['dead_est']:
        marks.append(f"~{row['dead_est']} ушло в DLX")
    return (f"{row['name']:<44} {row['type']:<8} msgs={row['messages']:<5} "
            f"ready={row['ready']:<5} unacked={row['unacked']:<4} cons={row['consumers']:<3} "
            f"pub={row['publish']:<7} get={row['get']:<7} deliver={row['deliver']:<7} "
            f"ack={row['ack']:<7} redeliver={row['redeliver']:<5} not_acked={row['not_acked']:<5}"
            + ('  ' + '; '.join(marks) if marks else ''))


def _get(one: dict, path: str):
    """GET management-API среды; ошибка — RuntimeError с текстом брокера."""
    got, err = rabbitmq_mgmt_get(one['host'], path, one['user'], one['password'], one['port'],
                                 scheme='https')
    if err:
        raise RuntimeError(err)
    return got


def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--env', required=True)
    ap.add_argument('--vhost', required=True)
    ap.add_argument('--queue', action='append', default=[], help='повторяемый; пусто — все')
    ap.add_argument('--config', default='')
    args = ap.parse_args()
    try:
        rows = rabbitmq_stats_queues(args.env, args.vhost, args.queue,
                                     rabbitmq_fleet_conf(args.config))
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')
    for row in rows:
        print(rabbitmq_stats_format(row))


if __name__ == '__main__':
    _main()

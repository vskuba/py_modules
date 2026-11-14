"""
Заглянуть в сообщение очереди, не потеряв его.

`GET /api/queues/{vhost}/{queue}/get_messages` — коварный эндпоинт: с
`ack_requeue_false` он **удаляет** выданные сообщения. На живом прод-DLQ это
уже стоило расследования: первый peek показал вину, второй зашёл пустым —
доказательство исчезло. Здесь `ack_requeue_true` зашит в код и наружу не
отдаётся: сообщения возвращаются, но остаются в очереди.

## Что здесь

* `rabbitmq_peek(env, vhost, queue, count)` — head очереди payload'ами;
  `x-death` из properties раскладывается в читаемую `deaths`-ленту
  (`reason @ время -> очередь`) — кто и когда сбросил сообщение в DLQ.

⚠ Peek берёт **head** очереди (старейшие). «Последнее» сообщение — это когда
ready посчитан с конца; для DLQ обычно берут `count=ready` и смотрят хвост.
⚠ Peek увеличивает счётчик deliver'ов — в `message_stats` будет рост,
сообщения при этом целы.
⚠ **Облачный LB консолей (prod/stage/dev) `/get_messages` не пропускает** —
проверено 2026-11-16: 404 «Object Not Found» по всем трём средам, прямой
:15672 снаружи недоступен, dev/stage за Cloudflare ещё и режут не-браузерный
UA (1010). Связка работала до 2026-11-14 — видимо, платформу подкрутили.
Тело DLQ-сообщения ищи в opensearch-логах консьюмера (там то же самое в
`jsonPayload.message`), либо зови оттуда, где :15672 достижим (docker-брокер,
in-cluster).

Конфиг доступов — тот же, что у `rabbitmq_fleet` (`~/.config/rabbitmq_mgmt.json`).

CLI:

    python3 -m rabbitmq_.rabbitmq_peek --env prod --vhost cashier_accounts \\
        --queue apm_transaction_notification_dl --count 2
"""
import argparse
import json
import urllib.parse

from rabbitmq_.rabbitmq_ import rabbitmq_mgmt_get
from rabbitmq_.rabbitmq_fleet import rabbitmq_fleet_conf

PEEK_TRUNCATE = 8192  # payload длиннее обрежется — для DLQ-разборов хватает с запасом


def rabbitmq_peek(env: str, vhost: str, queue: str, count: int = 1,
                  conf: dict | None = None) -> list:
    """Head очереди словами: [{'payload','routing_key','exchange','deaths'}].

    Args:
        env: среда из конфига (`prod`/`stage`/`dev`).
        vhost: виртуальный хост.
        queue: очередь (обычная или DLQ).
        count: сколько сообщений забрать (head, oldest-first).

    Returns:
        список сообщений; `payload` — текст (utf-8), `deaths` — лента смертей
        из `x-death` ([] у живых сообщений).

    ⚠ `ack_requeue_true` зашит: сообщения остаются в очереди. Не добавляй
    параметр «забрать навсегда» — он уже стоил одному DLQ доказательств.
    """
    conf = conf or rabbitmq_fleet_conf()
    if env not in conf:
        raise RuntimeError(f'среды {env!r} нет в конфиге; есть: '
                           f'{", ".join(sorted(conf))}')
    one = conf[env]
    path = (f'queues/{urllib.parse.quote(vhost, safe="")}/'
            f'{urllib.parse.quote(queue, safe="")}/get_messages'
            f'?count={int(count)}&ack_requeue_true=true&encoding=auto'
            f'&truncate={PEEK_TRUNCATE}')
    got, err = rabbitmq_mgmt_get(one['host'], path, one['user'], one['password'],
                                 one['port'], scheme='https')
    if err and 'HTTP 404' in err:
        err += ' — вероятно, LB консоли не пропускает /get_messages ' \
               '(см. карту ловушек в докстринге); тело ищи в opensearch-логах'
    if err:
        raise RuntimeError(err)
    return [{'payload': m.get('payload', ''),
             'payload_encoding': m.get('payload_encoding', ''),
             'routing_key': m.get('routing_key', ''),
             'exchange': m.get('exchange', ''),
             'redelivered': m.get('redelivered', False),
             'deaths': _deaths(m)}
            for m in (got or [])]


def _deaths(msg: dict) -> list:
    """`properties.x-death` -> ['reason @ время -> очередь'] (свежие первыми)."""
    out = []
    for entry in (msg.get('properties') or {}).get('x-death') or []:
        for queue, events in entry.items():
            for ev in (events or []):
                out.append(f"{ev.get('reason', '?')} @ {ev.get('time', '?')} -> {queue}")
    return out


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('--env', required=True)
    _P.add_argument('--vhost', required=True)
    _P.add_argument('--queue', required=True)
    _P.add_argument('--count', type=int, default=1)
    _P.add_argument('--config', default='')
    _C = _P.parse_args()
    try:
        msgs = rabbitmq_peek(_C.env, _C.vhost, _C.queue, _C.count,
                             rabbitmq_fleet_conf(_C.config))
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')
    for n, m in enumerate(msgs, 1):
        print(f'--- сообщение {n}/{len(msgs)} '
              f'(routing_key={m["routing_key"]!r} exchange={m["exchange"]!r} '
              f'redelivered={m["redelivered"]})')
        for line in m['deaths']:
            print(f'    смерть: {line}')
        payload = m['payload']
        try:  # json красиво, остальное как есть
            payload = json.dumps(json.loads(payload), ensure_ascii=False, indent=1)
        except ValueError:
            pass
        print(payload)
    if not msgs:
        print('пусто: в head очереди сообщений нет')
    raise SystemExit(0)

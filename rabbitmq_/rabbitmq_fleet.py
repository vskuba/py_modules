"""
Кластеры RabbitMQ: обзор очередей по prod/stage/dev одним вызовом.

Облачные консоли устроены иначе, чем docker-rabbitmq из `rabbitmq_`:
у каждой среды **свой хост и свой набор кредов** (продакшеновские на dev/stage
дают 401, и это молча «нет доступа», а не подсказка), а ещё глобальный
`/api/queues` без vhost прод не отдаёт (401), зато по vhost работает — модуль
ходит вторым путём. Всё это нащупывалось три вечера ручным curl при чистке
зомби-очередей (ноябрь 2026); здесь — чтобы одним запуском вместо скриптов
в /tmp.

## Что здесь

* `rabbitmq_fleet_conf` — конфиг доступов (env-файл `~/.config/rabbitmq_mgmt.json`);
* `rabbitmq_fleet_vhosts` — список vhost среды;
* `rabbitmq_fleet_queues` — список очередей vhost (name/ready/consumers);
* `rabbitmq_fleet_scan` — обход `envs × vhosts` с сухим прогоном паттерна:
  помечает, что сотрёт `queue:remove <vhost> --pattern=...` (тот же PCRE-вид
  `/^dod/` принимается как есть), и режим `--zombies` — консьюмеров ноль.

⚠ Сухой прогон ничего не удаляет — удаление только deploy-job'ом
(`vendor/bin/rabbit queue:remove` в helm hook), так исторически решил хозяин.

## Конфиг `~/.config/rabbitmq_mgmt.json`

    {
      "prod":  {"host": "rabbitmq.ipaygateway.com",      "user": "...", "password": "***"},
      "stage": {"host": "rabbitmq.stage-ipaygateway.com", "user": "...", "password": "***"},
      "dev":   {"host": "rabbitmq.dev-ipaygateway.com",   "user": "...", "password": "***"}
    }

⚠ Сертификат консолей внутренний — для https проверка TLS отключена
(см. `rabbitmq_mgmt_request`, scheme='https').

CLI:

    # что сотрёт паттерн по трём средам (dry-run queue:remove)
    python3 -m rabbitmq_.rabbitmq_fleet --pattern '/^(dod|replenishment)[.]/'
    # мёртвые очереди (cons=0, ready>0) конкретного vhost
    python3 -m rabbitmq_.rabbitmq_fleet --envs prod --vhosts cashier_accounts --zombies

Код выхода: 0 — находок нет, 10 — находки есть (удобно в проверках), 1 — ошибка.
"""
import argparse
import json
import os
import re

from rabbitmq_.rabbitmq_ import rabbitmq_mgmt_get

CONFIG_PATH = os.path.expanduser('~/.config/rabbitmq_mgmt.json')
FLEET_PORT = 443  # консоли кластеров сидят на 443, не на 15672


def rabbitmq_fleet_conf(path: str = '') -> dict:
    """Конфиг сред из json-файла (см. докстринг модуля).

    Args:
        path: файл конфига; пусто — `CONFIG_PATH`.

    Returns:
        {env: {'host', 'user', 'password', 'port'}}.
    """
    path = path or CONFIG_PATH
    if not os.path.isfile(path):
        raise RuntimeError(f'нет конфига {path}; создай по образцу из докстринга '
                           'rabbitmq_fleet (ключи prod/stage/dev)')
    try:
        with open(path, encoding='utf-8') as fh:
            conf = json.load(fh)
    except ValueError as exc:
        raise RuntimeError(f'{path} не читается как json: {exc}') from exc
    out = {}
    for env, one in conf.items():
        if not one.get('host'):
            raise RuntimeError(f'{path}: среде {env!r} не хватает host')
        out[env] = {'host': one['host'], 'user': one.get('user', ''),
                    'password': one.get('password', ''),
                    'port': int(one.get('port', FLEET_PORT))}
    return out


def rabbitmq_fleet_vhosts(env: str, conf: dict | None = None) -> list:
    """Список vhost среды (GET /api/vhosts)."""
    conf = conf or rabbitmq_fleet_conf()
    if env not in conf:
        raise RuntimeError(f'среды {env!r} нет в конфиге; есть: '
                           f'{", ".join(sorted(conf))}')
    one = conf[env]
    got, err = rabbitmq_mgmt_get(one['host'], 'vhosts', one['user'], one['password'],
                                 one['port'], scheme='https')
    if err:
        raise RuntimeError(err)
    return [v['name'] for v in (got or [])]


def rabbitmq_fleet_queues(env: str, vhost: str, conf: dict | None = None) -> list:
    """Очереди vhost: [{'name','ready','consumers','unacked','state'}].

    ⚠ Идём строго `/api/queues/<vhost>`: глобальный `/api/queues` прод отвечает
    401 даже с рабочими кредями (права выдаются по vhost).
    """
    conf = conf or rabbitmq_fleet_conf()
    if env not in conf:
        raise RuntimeError(f'среды {env!r} нет в конфиге; есть: '
                           f'{", ".join(sorted(conf))}')
    one = conf[env]
    got, err = rabbitmq_mgmt_get(one['host'], f'queues/{vhost}', one['user'],
                                 one['password'], one['port'], scheme='https')
    if err:
        raise RuntimeError(err)
    return [{'name': q.get('name', ''),
             'ready': q.get('messages_ready', 0),
             'unacked': q.get('messages_unacknowledged', 0),
             'consumers': q.get('consumers', 0),
             'state': q.get('state', '')}
            for q in (got or [])]


def rabbitmq_fleet_scan(envs: str = 'prod,stage,dev', vhosts: str = '',
                        pattern: str = '', zombies: bool = False,
                        conf: dict | None = None) -> list:
    """Обход envs × vhosts: что под паттерном и кто без консьюмеров.

    Args:
        envs: список сред через запятую.
        vhosts: vhost через запятую; пусто — все vhost каждой среды.
        pattern: regex или PCRE-вид `/^re/` (разделители снимаются); пусто — без фильтра.
        zombies: True — оставлять только консьюмеров=0 и ready>0.

    Returns:
        строки [{'env','vhost','name','ready','consumers','match'}].
    """
    conf = conf or rabbitmq_fleet_conf()
    rx = _compile(pattern) if pattern else None
    rows = []
    for env in [e.strip() for e in envs.split(',') if e.strip()]:
        targets = ([v.strip() for v in vhosts.split(',') if v.strip()]
                   or rabbitmq_fleet_vhosts(env, conf))
        for vhost in targets:
            for q in rabbitmq_fleet_queues(env, vhost, conf):
                match = bool(rx and rx.search(q['name']))
                if zombies and not (q['consumers'] == 0 and q['ready'] > 0):
                    continue
                if pattern and not match:
                    continue
                rows.append({'env': env, 'vhost': vhost, **q, 'match': match})
    return rows


def _compile(pattern: str) -> re.Pattern:
    """PCRE-делители toolkit'а ('/^dod/') python'у не нужны — снимаем."""
    body = pattern[1:-1] if len(pattern) > 1 and pattern.startswith('/') \
        and pattern.endswith('/') else pattern
    try:
        return re.compile(body)
    except re.error as exc:
        raise RuntimeError(f'паттерн не компилируется: {exc}') from exc


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('--envs', default='prod,stage,dev')
    _P.add_argument('--vhosts', default='', help='через запятую; пусто — все vhost среды')
    _P.add_argument('--pattern', default='', help='regex или /^pcre/ — как для queue:remove')
    _P.add_argument('--zombies', action='store_true', help='только cons=0 и ready>0')
    _P.add_argument('--config', default='')
    _C = _P.parse_args()
    try:
        found = rabbitmq_fleet_scan(_C.envs, _C.vhosts, _C.pattern, _C.zombies,
                                    rabbitmq_fleet_conf(_C.config))
    except RuntimeError as exc:
        raise SystemExit(f'ошибка: {exc}')
    for r in found:
        print(f"{r['env']:<6} {r['vhost']:<26} {r['name']:<48} "
              f"ready={r['ready']:<5} cons={r['consumers']}{'  <== паттерн' if r['match'] else ''}")
    print(f'находок: {len(found)}')
    raise SystemExit(10 if found else 0)

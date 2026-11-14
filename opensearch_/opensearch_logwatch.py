"""
Логосверка за окно: только НЕзнакомые подписи ошибок.

Смотреть логи глазами после каждого деплоя — путь к усталости и к пропуску:
шум (Jaeger без key, легальные RequestException) заслоняет новое. Здесь —
сверка без глаз: снять error/critical за окно из всех сред, сжать сообщения
до подписей, отнять baseline известных — напечатать только незнакомое.
Новая подпись после деплоя = смотреть сюда первым делом.

## Как сжимается подпись

uuid → `'u'`, числа → `N`, кавычки с содержимым → `'x'`, плюс локация из
`context.exception` — `[класс @ файл:строка]` (без vendor/-пути). Одинаковая
беда с разными uuid/суммами сворачивается в одну строку.

## Baseline

`logwatch_known.txt` рядом с модулем, строчки `regex_сервиса|фрагмент`.
Нашёл новое, убедил, что легальное — дописываешь строку (`--add`) и она
больше не шумит. Файл в репозитории — baseline переживает /tmp.

## Что здесь

* `opensearch_logwatch_sig(src)` — подпись одного хита (service, подпись);
* `opensearch_logwatch(...)` — сверка; вернуть {'new','known','samples'}:
  счётчики новых и известных подписей + число снятых хитов по индексам.

⚠ Окно и size — на одно: при `size` меньше числа событий окна unseen-часть
молча не попадёт в статистику (агрегации здесь нет, это выборка).
⚠ Уровень берём из `jsonPayload.logLevel.keyword` — честного, не из severity.

CLI:

    python3 -m opensearch_.opensearch_logwatch --window 2h
    # дописать легальное в baseline
    python3 -m opensearch_.opensearch_logwatch --add 'my-service|Some Exception'

Код выхода: 0 — чисто, 3 — есть незнакомые подписи.
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

from opensearch_.opensearch_ import opensearch_hits

KNOWN_FILE = str(Path(__file__).with_name('logwatch_known.txt'))
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NUM = re.compile(r'\b\d+\b')
_QUOT = re.compile(r"[\"'`][^\"'`]{0,60}[\"'`]")


def opensearch_logwatch_sig(src: dict) -> tuple:
    """Подпись хита: (сервис, нормализованное сообщение + локация)."""
    jp = src.get('jsonPayload', {})
    msg = jp.get('message') or ''
    s = _QUOT.sub("'x'", _UUID.sub("'u'", msg))
    s = ' '.join(_NUM.sub('N', s).split())[:130]
    loc = ''
    ctx = jp.get('context')
    if isinstance(ctx, str) and ctx.startswith('{'):
        try:
            exc = json.loads(ctx).get('exception', {})
            if exc:
                f = (exc.get('file') or '').split('/vendor/')[-1].split('/usr/src/app/')[-1]
                loc = f" [{exc.get('class', '?')} @ {f}:{exc.get('line', '?')}]"
        except (ValueError, AttributeError):
            pass
    return jp.get('serviceName') or '?', s + loc


def opensearch_logwatch(window: str = '2h', envs=('prod', 'stage', 'dev'),
                        index_fmt: str = 'cashier-{env}-fpm-*',
                        levels=('ERROR', 'CRITICAL'), known_file: str = '',
                        size: int = 1500, env: dict | None = None) -> dict:
    """Сверка логов за окно по средам, минус baseline.

    Args:
        window: промежуток для range (`2h`, `30m`, `1d`) — пишется как `now-<window>`.
        envs: имена сред; индекс собирается из `index_fmt`.
        index_fmt: шаблон индекса с `{env}`.
        levels: уровни logLevel.
        known_file: baseline; пусто — `KNOWN_FILE` рядом с модулем.
        size: потолок хитов на индекс (окно и size — на одно, см. докстринг).
        env: конфиг opensearch_ (cookie-сессия); пусто — по умолчанию модуля.

    Returns:
        {'new': Counter{(svc, sig): n}, 'known': Counter{ключ: n},
         'samples': {индекс: снято хитов}}.
    """
    query = json.dumps({'bool': {'filter': [
        {'terms': {'jsonPayload.logLevel.keyword': list(levels)}},
        {'range': {'timestamp': {'gte': f'now-{window}'}}},
    ]}})
    known = _known(known_file)
    new, tot_known, samples = Counter(), Counter(), {}
    for name in envs:
        index = index_fmt.format(env=name)
        hits = opensearch_hits(index, query, env, sort='timestamp', size=size)
        samples[index] = len(hits)
        for h in hits:
            key = opensearch_logwatch_sig(h.get('_source', h))
            for n, (bsvc, bfrag) in enumerate(known):
                if re.search(bsvc, key[0]) and bfrag.strip() in key[1]:
                    tot_known[f'{bsvc}|{bfrag}'] += 1
                    break
            else:
                new[key] += 1
    return {'new': new, 'known': tot_known, 'samples': samples}


def _known(path: str = '') -> list:
    path = path or KNOWN_FILE
    if not Path(path).is_file():
        return []
    return [tuple(l.strip().split('|', 1)) for l in open(path, encoding='utf-8')
            if '|' in l]


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('--window', default='2h')
    _P.add_argument('--envs', default='prod,stage,dev')
    _P.add_argument('--index-fmt', default='cashier-{env}-fpm-*')
    _P.add_argument('--known', default='', help='baseline-файл; по умолчанию рядом с модулем')
    _P.add_argument('--size', type=int, default=1500)
    _P.add_argument('--add', default='', help='строка `сервис|фрагмент` в baseline и выход')
    _C = _P.parse_args()
    if _C.add:
        with open(_C.known or KNOWN_FILE, 'a', encoding='utf-8') as fh:
            fh.write(_C.add.rstrip('\n') + '\n')
        print(f'дописано в {_C.known or KNOWN_FILE}')
        raise SystemExit(0)
    got = opensearch_logwatch(_C.window, tuple(e for e in _C.envs.split(',') if e),
                              _C.index_fmt, known_file=_C.known, size=_C.size)
    for (svc, sig), n in got['new'].most_common(40):
        print(f'{n:6}  [{svc}]  {sig[:110]}')
    if not got['new']:
        print(f"(чисто: за {_C.window} нового нет; известных: "
              f"{sum(got['known'].values())})")
    print('# known:', ', '.join(f'{k}={v}' for k, v in got['known'].most_common(8)) or 'none')
    raise SystemExit(3 if got['new'] else 0)

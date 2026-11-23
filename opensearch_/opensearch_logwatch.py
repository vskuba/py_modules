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

## Индексы и среды

Шаблон индекса `index_fmt` с `{env}` — из `OPENSEARCH_INDEX_FMT` в `.env.local`
проекта или из `--index-fmt`; общего дефолта в слое нет, при пустом — отказ.

## Baseline

Baseline — файл проекта, в общий слой он не попадает: путь задаётся `--known`
или `OPENSEARCH_LOGWATCH_KNOWN`; нет ни того ни другого — сверка без baseline.
Строчки `regex_сервиса|фрагмент`. Нашёл новое, убедил, что легальное —
дописываешь строку (`--add`) и она больше не шумит. Формат —
`logwatch_known.example.txt` рядом с модулем.

## Что здесь

* `opensearch_logwatch_sig(src)` — подпись одного хита (service, подпись);
* `opensearch_logwatch(...)` — сверка; вернуть {'new','known','samples'}:
  счётчики новых и известных подписей + число снятых хитов по индексам.

⚠ Окно и size — на одно: при `size` меньше числа событий окна unseen-часть
молча не попадёт в статистику (агрегации здесь нет, это выборка).
⚠ Уровень берём из `jsonPayload.logLevel.keyword` — честного, не из severity.

CLI:

    # OPENSEARCH_INDEX_FMT (или --index-fmt) заданы в проекте
    python3 -m opensearch_.opensearch_logwatch --window 2h
    python3 -m opensearch_.opensearch_logwatch --index-fmt 'myapp-{env}-app-*' \\
        --known /path/to/project/logwatch_known.txt --add 'my-service|Some Exception'

Код выхода: 0 — чисто, 3 — есть незнакомые подписи.
"""
import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path

from opensearch_.opensearch_ import opensearch_hits

# baseline живёт в проекте: путь — из OPENSEARCH_LOGWATCH_KNOWN или --known;
# формат строк — в logwatch_known.example.txt рядом с модулем
KNOWN_FILE = os.environ.get('OPENSEARCH_LOGWATCH_KNOWN', '')
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
                        index_fmt: str = '', levels=('ERROR', 'CRITICAL'), known_file: str = '',
                        size: int = 1500, env: dict | None = None) -> dict:
    """Сверка логов за окно по средам, минус baseline.

    Args:
        window: промежуток для range (`2h`, `30m`, `1d`) — пишется как `now-<window>`.
        envs: имена сред; индекс собирается из `index_fmt`.
        index_fmt: шаблон индекса с `{env}`; пусто — `OPENSEARCH_INDEX_FMT`,
            и если там пусто — отказ (общего дефолта в слое нет).
        levels: уровни logLevel.
        known_file: baseline; пусто — `OPENSEARCH_LOGWATCH_KNOWN`, там пусто —
            сверка без baseline.
        size: потолок хитов на индекс (окно и size — на одно, см. докстринг).
        env: конфиг opensearch_ (cookie-сессия); пусто — по умолчанию модуля.

    Returns:
        {'new': Counter{(svc, sig): n}, 'known': Counter{ключ: n},
         'samples': {индекс: снято хитов}}.
    """
    fmt = index_fmt or os.environ.get('OPENSEARCH_INDEX_FMT', '')
    if not fmt:
        raise SystemExit("шаблон индекса не задан: index_fmt=/--index-fmt или "
                         "OPENSEARCH_INDEX_FMT в .env.local проекта — общего дефолта в слое нет")
    query = json.dumps({'bool': {'filter': [
        {'terms': {'jsonPayload.logLevel.keyword': list(levels)}},
        {'range': {'timestamp': {'gte': f'now-{window}'}}},
    ]}})
    known = _known(known_file)
    new, tot_known, samples = Counter(), Counter(), {}
    for name in envs:
        index = fmt.format(env=name)
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
    if not path or not Path(path).is_file():
        return []
    return [tuple(l.strip().split('|', 1)) for l in open(path, encoding='utf-8')
            if '|' in l]


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('--window', default='2h')
    _P.add_argument('--envs', default='prod,stage,dev')
    _P.add_argument('--index-fmt', default='',
                    help='шаблон с {env}; по умолчанию OPENSEARCH_INDEX_FMT, там пусто — отказ')
    _P.add_argument('--known', default='',
                    help='baseline-файл; по умолчанию OPENSEARCH_LOGWATCH_KNOWN, там пусто — без baseline')
    _P.add_argument('--size', type=int, default=1500)
    _P.add_argument('--add', default='', help='строка `сервис|фрагмент` в baseline и выход')
    _C = _P.parse_args()
    if _C.add:
        _known_file = _C.known or KNOWN_FILE
        if not _known_file:
            raise SystemExit('--add: baseline-файл не задан (--known или OPENSEARCH_LOGWATCH_KNOWN)')
        with open(_known_file, 'a', encoding='utf-8') as fh:
            fh.write(_C.add.rstrip('\n') + '\n')
        print(f'дописано в {_known_file}')
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

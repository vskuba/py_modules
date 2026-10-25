"""Проба поддоменами: где живой хост, а где wildcard-DNS рисует фантомы.

В разведке сети вопрос «а есть ли у домена dev/test/stage/shop» возникает
постоянно, и ответ почти всегда одинаково обманчив: зона с wildcard (`*.домен`)
отдаёт запись на **каждое** придуманное имя, и наивный перебор рапортует
«стагинг!» на восемь имён из восьми. Живой хост познаётся не по факту записи,
а по адресу: он смотрит **не** на IP самого домена. Здесь эта пара шагов —
один вызов: снять A apex, пройтись словом × домен, разложить на «wildcard»
и «настоящее».

## Что здесь есть

* `net_sub_probe` — пачка доменов × слова-пробы; по каждому домену вердикт
  `wildcard` и список живых поддоменов (тех, что смотрят на чужой IP).

⚠ Резолвящееся имя — не хост: сверяй IP, а не запись. ⚠ Wildcard-домен тоже
может держать живой поддомен поверх (`shop.vsedodomu` на чужой машине при
дикой зоне) — сравнение с apex это ловит, сравнение «с собой вчерашним» нет.
"""
from concurrent.futures import ThreadPoolExecutor

import httpx

DOH = 'https://dns.google/resolve'
SUB_WORDS = ('dev', 'test', 'stage', 'staging', 'shop', 'm', 'cdn', 'old',
             'admin', 'api', 'new', 'demo', 'beta', 'www')


def net_sub_probe(domains, *, words=SUB_WORDS, timeout: float = 8.0,
                  workers: int = 24) -> dict:
    """Домены × слова-пробы: где wildcard, где живой хост на чужом IP.

    Args:
        domains: список доменов.
        words: пробы-поддомены (по умолчанию частая карта).
        timeout: секунд на ответ DoH.
        workers: параллельных запросов.

    Returns:
        {'ok': хоть где-то нашлось, 'rows': [{'domain',
         'apex': [IP apex], 'wildcard': bool,   # все пробы = адрес apex
         'phantom': [имена, совпадающие с apex],
         'live': [{'sub','a'}]                  # пробы с чужим адресом
         }]}
    """
    names = [d for d in domains] + [f'{w}.{d}' for d in domains for w in words]
    with ThreadPoolExecutor(workers) as pool:
        got = list(pool.map(lambda n: _net_sub_a(n, timeout), names))
    a_by_name = dict(zip(names, got))
    rows = []
    for d in domains:
        apex = set(a_by_name.get(d, []))
        phantom, live = [], []
        for w in words:
            sub = f'{w}.{d}'
            a = a_by_name.get(sub, [])
            if not a:
                continue
            if apex and set(a) <= apex:
                phantom.append(sub)
            else:
                live.append({'sub': sub, 'a': a})
        wildcard = len(phantom) >= max(2, len(words) * 2 // 3)
        rows.append({'domain': d, 'apex': sorted(apex), 'wildcard': wildcard,
                     'phantom': phantom, 'live': live})
    return {'ok': any(r['live'] for r in rows), 'rows': rows}


def _net_sub_a(name: str, timeout: float) -> list:
    """A-записи имени через DoH; CNAME разворачивается до адресов."""
    try:
        r = httpx.get(DOH, params={'name': name, 'type': 'A'}, timeout=timeout)
        js = r.json()
        return [a['data'] for a in js.get('Answer', [])
                if a.get('type') == 1 and a.get('data')]
    except (httpx.HTTPError, ValueError):
        return []


if __name__ == '__main__':
    import argparse

    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('domains', nargs='+')
    _P.add_argument('--words', default=','.join(SUB_WORDS))
    _C = _P.parse_args()
    _res = net_sub_probe(_C.domains, words=_C.words.split(','))
    for _r in _res['rows']:
        print(f"{_r['domain']:<26} apex={','.join(_r['apex']) or '-':<16} "
              f"wildcard={'да' if _r['wildcard'] else 'нет':<3} "
              f"фантомов={len(_r['phantom'])}")
        for _l in _r['live']:
            print(f"    ЖИВОЙ {_l['sub']:<34} -> {','.join(_l['a'])}")
    raise SystemExit(0 if _res.get('ok') else 1)

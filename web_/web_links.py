"""Живые ссылки: ни одного факта без открытой ссылки — и проверка, что она открыта.

Каждый факт в разведочных доках снабжён ссылкой, но ссылки умирают молча: статью
снимают, реестр меняет адрес, архив теряет снимок. После десятого захода «всё
живое» уже ничего не значит — ссылки помнятся, а не проверены. Здесь — перепроверка:
все внешние ссылки `.md` и `.html` обходятся GET'ом пачкой, параллельно, с честным
UA: тот же, что ходит в википедию (браузерный UA она отшивает, контактному — верит).

## Что здесь есть

* `web_links_check` — перепись ссылок по файлам: статус, живой/мёртвый, и
  отдельная стопка «подтверди руками».

⚠ 401/403/429 и весь 5xx — не смерть: вики и реестра проверяют вызывающего,
crt.sh перегружен; они идут в `ask`, живость решает человек (или другой UA).
⚠ Ссылка за логином не откроется ничем — та же стопка `ask`, это не баг проверки.
"""
import argparse
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

WEB_LINKS_URL = re.compile(r'https?://[^\s<>"\'\\)`…]+')
WEB_LINKS_TRIM = '.,;:!?'
# Настоящий хост: между `://` и первым `/` есть точка — отсечь «https://www»
# из обрывка фразы и голые внутренние имена.
WEB_LINKS_HOST = re.compile(r'https?://[^/]*\.', re.I)
# Честный UA с контактом: так просят ходить фонды и реестры; браузерный они
# считают ботом и дают 403 — проверка выдала бы мёртвыми живые ссылки.
WEB_LINKS_UA = 'research-script/1.0 (contact: intel@example.com)'


def web_links_check(paths: list, *, workers: int = 8,
                     timeout: float = 20.0) -> dict:
    """Каждой внешней ссылке в файлах — живой статус; пачкой и параллельно.

    Args:
        paths: файлы `.md`/`.html` (директории не обходятся — зовите список).
        workers: одновременных запросов.
        timeout: секунд на запрос (редиректы включены).

    Returns:
        {'ok': мёртвых нет, 'checked': сколько уникальных ссылок,
         'rows': [{'url', 'status', 'kind': ok|ask|dead, 'note', 'where'}],
         'dead': […], 'ask': […]} — `where` первые три адреса файл:строка.

    ⚠ Одна ссылка проверяется один раз, где бы ни встречалась; `where` — все
    места, до трёх.
    """
    seen: dict[str, list] = {}
    for p in paths:
        try:
            text = open(p, encoding='utf-8').read()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in WEB_LINKS_URL.finditer(line):
                # в href HTML-карты `&` живёт как `&amp;` — снимаем сущности,
                # иначе запрос уедет с мусором в query.
                url = html.unescape(m.group()).rstrip(WEB_LINKS_TRIM)
                # `)` в класс не входит (в прозе это чаще конец фразы), но
                # заголовок Википедии живёт в скобках: «Ariete_(azienda)» —
                # возвращаем скобку, если внутри matches её пара открыта.
                if url.count('(') > url.count(')'):
                    url += ')'
                # «https://www» из обрывка фразы — не адрес: у настоящего
                # хоста всегда есть точка.
                if WEB_LINKS_HOST.search(url):
                    seen.setdefault(url, []).append(f'{p}:{n}')
    urls = sorted(seen)
    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(lambda u: _web_links_get(u, timeout), urls))
    for r in rows:
        r['where'] = ','.join(seen[r['url']][:3])
    return {'ok': not any(r['kind'] == 'dead' for r in rows),
            'checked': len(rows), 'rows': rows,
            'dead': [r for r in rows if r['kind'] == 'dead'],
            'ask': [r for r in rows if r['kind'] == 'ask']}


def _web_links_get(url: str, timeout: float, retries: int = 4) -> dict:
    """Одна ссылка: GET до финального ответа; коннект-ошибку повторяем.

    Веб-архив и многие витрины режут одновременные соединения с одного адреса:
    ConnectError в параллельном прогоне — чаще троттлинг, чем смерть (снимок
    web.archive.org отвечает и за 2 с, и отказом подряд). Перед приговором
    «мёртв» даём `retries` заходов с нарастающей паузой. HTTP-код (404)
    повтором не лечим — он настоящий.
    """
    for n in range(retries):
        if n:
            time.sleep(min(1.5 * 2 ** (n - 1), 8.0))
        try:
            r = httpx.get(url, headers={'User-Agent': WEB_LINKS_UA},
                          follow_redirects=True, timeout=timeout)
            status = r.status_code
            note = '' if status < 400 or status in (401, 403, 405, 429) \
                else f'HTTP {status}'
            break
        except httpx.TimeoutException as e:
            status, note = 0, type(e).__name__
        except httpx.HTTPError as e:
            status, note = 0, type(e).__name__
    # 4xx (кроме «я тебя не пущу») — страницы точно нет: это смерть. 5xx —
    # сервер есть, но сейчас барахлит (crt.sh, витрины под нагрузкой): не приговор.
    kind = ('dead' if status == 0 else
            'ok' if status < 400 else
            'ask' if status in (401, 403, 405, 429) or status >= 500 else 'dead')
    return {'url': url, 'status': status, 'kind': kind, 'note': note}


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('files', nargs='+')
    _P.add_argument('--workers', type=int, default=8)
    _a = _P.parse_args()
    _res = web_links_check(_a.files, workers=_a.workers)
    for _r in _res['rows']:
        if _r['kind'] != 'ok':
            print(f"{_r['kind']:<4} {_r['status'] or '-':<4} {_r['url']} "
                  f"[{_r['note']}] в {_r['where']}")
    print(f"проверено: {_res['checked']}, мёртвых: {len(_res['dead'])}, "
          f"подтверди руками: {len(_res['ask'])}")
    raise SystemExit(0 if _res['ok'] else 1)

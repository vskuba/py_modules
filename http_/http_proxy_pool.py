"""Пул прокси: скачать пачку, проверить пачку, давать чистый IP на каждую попытку.

`http_proxy.py` умеет разобрать строку провайдера и проверить один адрес. Здесь —
следующий шаг: когда прокси берут из **бесплатных листов** (их десятки, живых в них
менее половины) и когда каждый запрос должен уйти со своего адреса, потому что сайт
считает попытки по IP.

## Что здесь есть

* `http_proxy_pool_fetch` — вытащить список `ip:port` из публичных лист-апи;
* `http_proxy_pool_alive` — параллельно прогреть список до нужной цели и оставить
  живые именно для неё (не «прокси вообще», а «до этого сайта»; лист деградирует
  за минуты);
* `http_proxy_pool_rotate` — счётчик попыток: следующий вызов выдаёт живой прокси,
  выгоревший (замок, таймауты) — помечает и обходит.

⚠ **Живой чужой прокси = чужой транспорт.** Ротация отдаёт адрес наружу строкой,
а вызывающий зовёт `http_pool_transport(proxy)`: транспорт под каждый выход свой,
и смешивать выходы в одном соединении нельзя (`http_pool.py`).
"""
import asyncio
import re
import urllib.parse

import httpx

HTTP_PROXY_POOL_SOURCES = (
    # лист-апи без ключа: geonode/openproxylist отдают JSON, proxyscrape — строки;
    # живых в листах ~5%, поэтому страниц несколько, а не одна
    'https://proxylist.geonode.com/api/proxy-list?limit=250&page=1'
    '&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps&anonymity=high%2Celite',
    'https://proxylist.geonode.com/api/proxy-list?limit=250&page=2'
    '&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps&anonymity=high%2Celite',
    'https://proxylist.openproxylist.org/api/v2/proxy/list'
    '/proxy_types%5B%5D=http&format=json&limit=300',
    'https://www.proxyscrape.com/v2/?request=displayproxies&protocol=http'
    '&timeout=6000&country=all&ssl=all&anonymity=elite',
    # гитхаб-листы: тысячи строк, обновляются каждые минуты, формат — строка на прокси
    'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt',
    'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
)


async def http_proxy_pool_fetch(sources: tuple = HTTP_PROXY_POOL_SOURCES,
                                limit: int = 0) -> dict:
    """Собрать `ip:port` из публичных лист-апи (JSON-массивы и текстовые листы).

    Args:
        sources: адреса лист-апи; JSON с полем ip/port и простой текст по строке
            на прокси разбираются оба.
        limit: сколько максимум строк забрать (0 — сколько дали).

    Returns:
        {'ok': хоть что-то приехало, 'count': сколько, 'proxies': ['ip:port', …],
         'errors': откуда не удалось прочитать}
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
        answers = await asyncio.gather(*(c.get(u) for u in sources),
                                       return_exceptions=True)
    proxies, errors = [], []
    for src, a in zip(sources, answers):
        try:
            raw = a.json()
            raw = [f"{p['ip']}:{p['port']}" for p in
                   (raw.get('data') or raw.get('hydra:member')
                    or raw.get('member') or [])]
        except (ValueError, AttributeError, KeyError, TypeError):
            try:
                # текстовые листы бывают 'ip:port', 'ip:port:страна' и с шапкой-комментом
                raw = re.findall(r'\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}', a.text)
            except AttributeError:
                errors.append(urllib.parse.urlsplit(str(src)).netloc)
                continue
        proxies += raw
        if not raw:
            errors.append(urllib.parse.urlsplit(str(src)).netloc)
    proxies = list(dict.fromkeys(proxies))  # источники пересекаются, дубли режем
    return {'ok': bool(proxies), 'count': len(proxies),
            'proxies': proxies[:limit or None], 'errors': errors}


async def http_proxy_pool_alive(proxies: list, probe: str, workers: int = 40,
                               timeout: float = 6.0) -> dict:
    """Оставить прокси, которые живы **до конкретной цели** (GET probe, своим клиентом).

    Args:
        proxies: список `ip:port`.
        probe: куда ходим (обычно главная мишень; GET — счётчики POST-входа не трогают).
        workers: сколько прогревок идёт разом.
        timeout: секунд на прокси; молчуна не ждём дольше.

    Returns:
        {'alive': ['ip:port', …], 'dead': [{'proxy':…, 'error':…}], 'checked': сколько}

    ⚠ Соединение на прогрев открывается заново: вопрос «работает ли сейчас»,
    а не «работало ли в пуле» — потому свой клиент, а не общий пул (`http_proxy_check`).
    """
    sem = asyncio.Semaphore(workers)

    async def one(p):
        async with sem:
            try:
                async with httpx.AsyncClient(proxy=f'http://{p}', timeout=timeout,
                                             follow_redirects=True) as c:
                    r = await c.get(probe, headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code == 200 and len(r.text) > 1000:
                    return p, ''
                return None, f'http={r.status_code} len={len(r.text)}'
            except Exception as e:
                return None, f'{type(e).__name__}'

    results = await asyncio.gather(*(one(p) for p in proxies))
    return {'alive': [p for p, e in results if p],
            'dead': [{'proxy': p, 'error': e} for p, e in results if not p],
            'checked': len(results)}


def http_proxy_pool_rotate(proxies: list, per_ip: int = 2) -> dict:
    """Счётчик попыток: `next()` даёт следующий живой прокси, `burn()` — сжигает сгоревший.

    Возвращает {'next': вызов -> 'http://ip:port' или '' (все сгорели),
    'burn': вызвать с адресом — прокси вычёркивается, 'left': сколько живо}.
    Обход по кругу: каждый прокси `per_ip` раздач, потом очередь по кругу
    (окно антибрутфорса на сайте обычно шире — не больше пары-тройки попыток на IP).
    """
    pool = {p if '://' in p else f'http://{p}': per_ip for p in proxies}

    def pick() -> str:
        for p in list(pool):
            if pool[p] > 0:
                pool[p] -= 1
                return p
        return ''

    def burn(proxy: str) -> None:
        pool[proxy] = 0

    return {'next': pick, 'burn': burn, 'left': lambda: sum(pool.values())}

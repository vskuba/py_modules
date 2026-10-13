"""Отпечаток чужого сайта: чем движок, какая версия, где точки входа и что течёт.

`web_peek` отдаёт сырой ответ, `web_peek_markers` ищет готовые подстроки. Здесь —
следующий вопрос: **на что это похоже**. Cookie-имя, путь роута, след темы говорят
про движок и версию больше, чем любой честный заголовок; а карта «маркер→движок»
одна и та что для OpenCart, что для WordPress — знать её один раз дешевле, чем
два раза собирать по крупицам руками.

## Что здесь есть

* `web_fingerprint` — набор зондов (главная, /admin/, подсказки путей) и карта
  маркеров → стек, версия, точки входа, признаки утечек; всё с доказательством.

⚠ Версия по маркерам — это **подобие**, а не факт: карта чинится по мере новых
граблей, и правится здесь, а не в проекте, где её собрали впервые.
"""
import asyncio

import httpx

# что искать в теле/куках → к какому стеку это относится
FP_MARKERS = {
    'OCSESSID': ('OpenCart', 'кука сессии OpenCart'),
    'catalog/view/theme': ('OpenCart', 'путь темы OpenCart'),
    'index.php?route=': ('OpenCart', 'маршрут ядра OpenCart'),
    'wp-content': ('WordPress', 'путь плагинов/тем WP'),
    'wp-json': ('WordPress', 'REST-корень WP'),
    'JSESSIONID': ('Java', 'кука сессии servlet'),
    'laravel': ('Laravel', 'след laravel'),
    'X-Powered-By': ('стек', 'заголовок движка'),
}

# версия OpenCart: есть /system/storage/ (2.3+/3) или нет (2.0–2.2)
FP_VERSION_TELL = ('/system/storage/',)


async def web_fingerprint(url: str, probes=('/', '/admin/', '/robots.txt'),
                          timeout: float = 12) -> dict:
    """Снять отпечаток сайта: движок и версия, точки входа, признаки утечек.

    Args:
        url: база сайта (без слэша на конце).
        probes: относительные пути-зонды; по каждому сверяем статус и маркеры.
        timeout: секунд на запрос.

    Returns:
        {'engine': имя или '?', 'version': подобие версии или '',
         'entries': {путь: код}, 'stack': [метка…], 'leaks': [путь…],
         'evidence': [{'probe','marker','why'}] — откуда вердикт}
    """
    url = url.rstrip('/')
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as c:
        answers = await asyncio.gather(
            *(c.get(url + p, headers={'User-Agent': 'Mozilla/5.0'}) for p in probes),
            return_exceptions=True)
    evidence, entries, stack, leaks = [], {}, [], []
    for path, a in zip(probes, answers):
        if isinstance(a, Exception):
            entries[path] = f'сбой: {type(a).__name__}'
            continue
        head = {k.lower(): v for k, v in a.headers.items()}
        body = a.text if len(a.text) < 400_000 else ''
        blob = body + ' '.join(f'{k}={v}' for k, v in head.items())
        entries[path] = a.status_code
        for mk, (engine, why) in FP_MARKERS.items():
            if mk.lower() in blob.lower():
                evidence.append({'probe': path, 'marker': mk, 'why': why})
                if engine not in stack and engine != 'стек':
                    stack.append(engine)
    engine = stack[0] if stack else '?'
    version = ''
    if 'OpenCart' in stack:
        hit = next((e for e in evidence if e['marker'] == 'index.php?route='), None)
        version = '2.x–3.x'
        storage = any(e['probe'] in FP_VERSION_TELL and e['marker']
                      for e in evidence)
        version = '2.3+/3' if storage else '2.0–2.2 (storage не виден)'
    for path, code in entries.items():
        if code not in (200, 301, 302) and not isinstance(code, str):
            continue
    leaks = [p for p, c in entries.items() if c == 200 and p != '/']
    return {'engine': engine, 'version': version, 'entries': entries,
            'stack': stack, 'leaks': leaks, 'evidence': evidence}

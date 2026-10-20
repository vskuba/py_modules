"""Cloudflare Turnstile: виджет на живой странице — найти, нажать, дождаться токен.

Вызов сидит в чужом iframe и часто ещё и в теневом корне: обычный селектор
его не видит, а `querySelectorAll('iframe')` по документу отвечает пустотой
даже тогда, когда человек видит перед собой чекбокс. Модуль закрывает три
грабли: найти сквозь тень, нажать мышью по центру (доверенное событие), и
объяснить по сети, почему токен так и не пришёл.

## ⚠ Токен ≠ успех

Поле ответа появляется на странице до прохождения вызова — пустое или
вчерашнее. Годится только непустой **после клика**; за этим и смотрит
`cloudflare_token_wait`.

## ⚠ Молчание громче ошибки

Непройденный вызов не даёт ни исключения, ни кода на странице: спиннер
сменяется исходным чекбоксом, консоль молчит. Причина видна только в сети —
её собирает `cloudflare_watch`, а разбирает `cloudflare_challenge_diagnose`.

Страницу и прокси модуль не заводит: ему приносят готовую `page` из
`browser_` или из своего CLI.
"""
import asyncio

# Хост вызова и поле, куда движок вписывает ответ.
CLOUDFLARE_WIDGET_HOST = 'challenges.cloudflare.com'
CLOUDFLARE_TOKEN_INPUT = 'input[name*="cf-turnstile-response"]'

# Короткая строка токеном не бывает: меньше этого числа — поле пустое.
CLOUDFLARE_TOKEN_MIN_LEN = 60

# Разумный предел прохождения: спиннер крутится секунды, минуты — признак
# того, что раунд пошёл по кругу.
CLOUDFLARE_TOKEN_TIMEOUT_MS = 60000
CLOUDFLARE_TOKEN_POLL_MS = 1000

# Признаки состояний внутри iframe. Подписи движок не переводит — смотрим на
# его английские слова; пустой текст (спиннер, галочка-картинка) так не
# классифицируется, для него остаются токен и сеть.
CLOUDFLARE_STATE_MARKS = (
    ('error', ('error', 'failed')),
    ('ok', ('success', 'verified')),
    ('checkbox', ('verify you are human',)),
)

# Обход документа с открытыми теневыми корнями: первый iframe вызова и его
# rect в системе вьюпорта — той же, которой говорит мышь.
CLOUDFLARE_WIDGET_JS = """
(host) => {
  const find = (root) => {
    for (const el of root.querySelectorAll('iframe'))
      if ((el.src || '').includes(host)) return el;
    for (const el of root.querySelectorAll('*')) {
      if (!el.shadowRoot) continue;
      const inner = find(el.shadowRoot);
      if (inner) return inner;
    }
    return null;
  };
  const f = find(document);
  if (!f) return null;
  f.scrollIntoView({block: 'center'});
  const r = f.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height, src: f.src};
}
"""


async def cloudflare_widget_box(page) -> dict | None:
    """Найти iframe вызова на странице сквозь тень; нет виджета — None.

    Args:
        page: страница Playwright (async API).

    Возвращает `{'x', 'y', 'w', 'h', 'src'}` — координаты вьюпорта, в центр
    которых и бьёт `cloudflare_widget_click`.
    """
    return await page.evaluate(CLOUDFLARE_WIDGET_JS, CLOUDFLARE_WIDGET_HOST)


async def cloudflare_widget_click(page) -> bool:
    """Кликнуть по центру чекбокса вызова мышью; False — виджета нет.

    Именно `mouse.click`: доверительное событие, которого хочет защита.
    `.click()` по элементу остаётся синтетическим и на managed-раунде
    кончается спиннером без токена.
    """
    box = await cloudflare_widget_box(page)
    if not box or box['w'] < 2:
        return False
    await page.mouse.click(box['x'] + box['w'] / 2, box['y'] + box['h'] / 2)
    return True


async def cloudflare_widget_state(page) -> str:
    """Состояние видимого виджета словами: 'checkbox' | 'ok' | 'error' | 'pending'.

    Читает текст iframe вызова насквозь через `page.frames`: cross-origin
    для JS здесь не преграда, Playwright сидит выше той политики. 'pending' —
    iframe есть, но текст не классифицируется (спиннер, галочка-картинка);
    пустая строка — iframe не найден. Окончательный ответ всё равно за
    токеном и сетью.
    """
    seen = False
    for frame in page.frames:
        if CLOUDFLARE_WIDGET_HOST not in frame.url:
            continue
        seen = True
        try:
            text = (await frame.text_content('body') or '').lower()[:200]
        except Exception:
            continue
        for state, marks in CLOUDFLARE_STATE_MARKS:
            if any(m in text for m in marks):
                return state
    return 'pending' if seen else ''


async def cloudflare_token_read(page) -> str:
    """Прочитать поле ответа вызова на странице; пусто — поля нет или он пуст."""
    return await page.evaluate(
        "(s) => { const i = document.querySelector(s);"
        " return i ? (i.value || '') : ''; }", CLOUDFLARE_TOKEN_INPUT)


async def cloudflare_token_wait(page, timeout_ms: int = CLOUDFLARE_TOKEN_TIMEOUT_MS,
                                poll_ms: int = CLOUDFLARE_TOKEN_POLL_MS) -> str:
    """Дожидаться непустого токена после клика; сорвался — пустая строка.

    Опрашивает поле, а не событие: успеха как события у вызова нет, есть
    только момент, когда движок вписывает ответ в форму.
    """
    left = timeout_ms / 1000
    while left > 0:
        token = await cloudflare_token_read(page)
        if len(token) >= CLOUDFLARE_TOKEN_MIN_LEN:
            return token
        await asyncio.sleep(poll_ms / 1000)
        left -= poll_ms / 1000
    return ''


def cloudflare_watch(page) -> dict:
    """Начать сбор ответов вызова на странице; вернуть состояние со слушателем.

    В `state['events']` — строки `{'url', 'status'}` без запросных токенов и
    тел: их хватает разбору, а протокол вызова — не то, что стоит хранить.
    Отвязка — `state['stop']()`; без неё слушатель переживёт сессию.
    """
    state: dict = {'events': []}

    def on_response(response):
        url = response.url.split('?', 1)[0]
        if CLOUDFLARE_WIDGET_HOST in url or '/cdn-cgi/challenge-platform/' in url:
            state['events'].append({'url': url[-120:], 'status': response.status})

    page.on('response', on_response)

    def stop():
        page.remove_listener('response', on_response)

    state['stop'] = stop
    return state


def cloudflare_challenge_diagnose(events: list, token: str = '') -> dict:
    """Объяснить по собранным ответам, почему вызов не дал токена.

    Args:
        events: строки из `cloudflare_watch(page)['events']`.
        token: токен со страницы; непустой перекрывает всё — вызов пройден.

    Правила выросли из живых прогонов. 401 на `/pat/` без последующего
    выпуска токена — браузеру нечем платить Private State Token: этого API
    нет в Chromium без приватных песочниц, и никакие маски отпечатка тут не
    помогают. Ответы `/fo/` с 4xx — страница вызова отклонена по очкам,
    вопрос к репутации адреса выхода, не к браузеру. 204 на `/i/` — норма,
    телеметрия дошла; отдельным правилом не считается.
    """
    if len(token) >= CLOUDFLARE_TOKEN_MIN_LEN:
        return {'verdict': 'passed', 'why': 'токен на странице'}
    if not events:
        return {'verdict': 'silent', 'why': 'ни одного ответа вызова'}
    if any(e['status'] == 401 and '/pat/' in e['url'] for e in events):
        return {'verdict': 'pat-missing',
                'why': '401 на redemption-вызове: браузеру нечем платить '
                       'Private State Token — нужен браузер с этим API'}
    if any('/fo/' in e['url'] and e['status'] >= 400 for e in events):
        return {'verdict': 'scored',
                'why': 'страница вызова отклонена: репутация адреса выхода'}
    return {'verdict': 'unknown',
            'why': f'{len(events)} ответов без известного признака'}


def _cli() -> None:
    """Разовый замер виджета: открыть адрес, найти, нажать, дождаться, объяснить."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog='cloudflare_.cloudflare')
    parser.add_argument('url')
    parser.add_argument('--proxy', default='')
    parser.add_argument('--timeout', type=int, default=60,
                        help='секунд ждать токен после клика')
    args = parser.parse_args()

    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                proxy={'server': args.proxy} if args.proxy else None)
            page = await browser.new_page()
            watch = cloudflare_watch(page)
            await page.goto(args.url, wait_until='domcontentloaded')
            await asyncio.sleep(3)
            clicked = await cloudflare_widget_click(page)
            token = await cloudflare_token_wait(page, args.timeout * 1000)
            state = await cloudflare_widget_state(page)
            watch['stop']()
            print(json.dumps({'clicked': clicked, 'state': state,
                              'token': bool(token),
                              'diagnose': cloudflare_challenge_diagnose(
                                  watch['events'], token)},
                             ensure_ascii=False))
            await browser.close()

    asyncio.run(run())


if __name__ == '__main__':
    _cli()

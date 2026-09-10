"""Ожидания по признаку, а не по часам.

Пауза в миллисекундах — самый дорогой вид ожидания в сценарии, потому что она
ошибается всегда: на быстрой странице тратит время впустую, на медленной не
дожидается. И второе хуже — сценарий падает через день после того, как его
написали и проверили, на чужой загруженной сети.

Здесь три ожидания, каждое со своим признаком конца:

  * `browser_wait_dom_stable` — **DOM перестал меняться.** Отвечает на вопрос «она
    закончила рисовать?». Именно его задают одностраничным приложениям: событие
    загрузки у них давно прошло, а список ещё пуст, потому что данные придут
    третьим запросом и отрисуются четвёртым.
  * `browser_wait_network_idle` — **страница перестала спрашивать сервер.** Тот же
    вопрос со стороны сети; годится там, где отрисовка идёт непрерывно (анимация,
    бегущий таймер) и покоя DOM не будет никогда.
  * `browser_wait_text` — **на странице появился текст.** Самый точный признак из
    трёх, когда известно, чего ждём: «Сохранено», имя вошедшего, номер заказа.

Приём тот же, что у замера покоя экрана телефона (`adb_step`): ждём не события, а
**окна тишины** — столько-то миллисекунд, в которые ничего не произошло. Событие
«загрузка кончилась» врёт, окно тишины — нет.
"""

import asyncio
import time

# Сколько миллисекунд без единого изменения считать покоем. Полсекунды: меньше
# ловит паузу между двумя кадрами отрисовки одного и того же списка, больше —
# добавляет полсекунды к каждому шагу сценария.
BROWSER_WAIT_QUIET_MS = 500

# Общий предел ожидания. Дольше ждать нечего: страница, которая не успокоилась за
# десять секунд, не успокоится и за минуту — она либо крутит анимацию, либо
# опрашивает сервер по кругу.
BROWSER_WAIT_TIMEOUT_MS = 10000

# Как часто проверяем, наступила ли тишина. 50 мс — на порядок мельче самого
# короткого разумного окна покоя, и при этом не жжёт процессор.
BROWSER_WAIT_POLL_MS = 50

# Наблюдатель покоя живёт внутри страницы: считать мутации из Python значило бы
# гнать через мост каждое изменение DOM — на живой странице это тысячи сообщений
# в секунду, и сам замер стал бы главной нагрузкой.
BROWSER_WAIT_STABLE_JS = """
([quiet, timeout, poll]) => new Promise((resolve) => {
  const started = performance.now();
  let last = started;
  let count = 0;

  const observer = new MutationObserver((records) => {
    count += records.length;
    last = performance.now();
  });
  observer.observe(document, {subtree: true, childList: true,
                              characterData: true, attributes: true});

  const tick = setInterval(() => {
    const now = performance.now();
    const calm = now - last >= quiet;
    if (!calm && now - started < timeout) return;

    clearInterval(tick);
    observer.disconnect();
    resolve({waited_ms: Math.round(now - started), mutations: count, calm: calm});
  }, poll);
})
"""


async def browser_wait_dom_stable(page, quiet_ms: int = BROWSER_WAIT_QUIET_MS,
                                  timeout_ms: int = BROWSER_WAIT_TIMEOUT_MS) -> dict:
    """
    Ждёт, пока DOM не перестанет меняться на `quiet_ms`.

    Returns:
        dict: `waited_ms` — сколько прождали, `mutations` — сколько изменений
        насчитали, `calm` — дождались тишины (`False` — вышел общий предел).

    ⚠ **Не дождаться — не ошибка.** Страница с анимацией или бегущими часами не
    успокоится никогда, и падать на ней значило бы запретить сценарии на половине
    современных сайтов. Вызывающий смотрит на `calm` и решает сам.

    Свой предел поверх страничного (`asyncio.wait_for`) — не перестраховка: обещание
    внутри страницы разрешает её же поток исполнения, и страница, вставшая намертво
    на своём JavaScript, не досчитает до конца никогда.
    """
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            page.evaluate(BROWSER_WAIT_STABLE_JS,
                          [quiet_ms, timeout_ms, BROWSER_WAIT_POLL_MS]),
            timeout=(timeout_ms + BROWSER_WAIT_QUIET_MS) / 1000)
    except Exception:
        return {'waited_ms': round((time.monotonic() - started) * 1000),
                'mutations': 0, 'calm': False}

    return {'waited_ms': int(result.get('waited_ms') or 0),
            'mutations': int(result.get('mutations') or 0),
            'calm': bool(result.get('calm'))}


async def browser_wait_network_idle(page, quiet_ms: int = BROWSER_WAIT_QUIET_MS,
                                    timeout_ms: int = BROWSER_WAIT_TIMEOUT_MS) -> dict:
    """
    Ждёт, пока страница не перестанет спрашивать сервер на `quiet_ms`.

    Считаем сами, а не `wait_for_load_state('networkidle')`, по двум причинам. Тот
    ждёт «ноль запросов в течение 500 мс» и не даёт ни настроить окно, ни узнать,
    дождался он или сдался; и он привязан к навигации — после клика по кнопке,
    которая не уводит страницу, его вызов возвращается сразу.

    Считаем **запросы в полёте**, а не все подряд: страница, которая раз в секунду
    опрашивает сервер, никогда не даст «ни одного запроса за полсекунды», но между
    опросами она в покое, и именно этот покой нужен.

    Returns:
        dict: `waited_ms`, `requests` — сколько запросов ушло за время ожидания,
        `idle` — дождались покоя.
    """
    state = {'flight': 0, 'total': 0, 'last': time.monotonic()}

    def on_request(_request):
        state['flight'] += 1
        state['total'] += 1
        state['last'] = time.monotonic()

    def on_done(_request):
        # Ниже нуля не опускаемся: запрос, начатый до нашей подписки, приходит сюда
        # без пары, и отрицательный счётчик держал бы ожидание до самого предела.
        state['flight'] = max(0, state['flight'] - 1)
        state['last'] = time.monotonic()

    page.on('request', on_request)
    page.on('requestfinished', on_done)
    page.on('requestfailed', on_done)

    started = time.monotonic()
    idle = False
    try:
        while (time.monotonic() - started) * 1000 < timeout_ms:
            quiet = (time.monotonic() - state['last']) * 1000
            if state['flight'] == 0 and quiet >= quiet_ms:
                idle = True
                break
            await asyncio.sleep(BROWSER_WAIT_POLL_MS / 1000)
    finally:
        # Слушателей снимаем обязательно: сессию переиспользуют, и оставленная
        # тройка досчитывала бы чужие запросы до конца жизни страницы.
        for event, handler in (('request', on_request), ('requestfinished', on_done),
                               ('requestfailed', on_done)):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass

    return {'waited_ms': round((time.monotonic() - started) * 1000),
            'requests': state['total'], 'idle': idle}


async def browser_wait_text(page, text: str, selector: str = '',
                            timeout_ms: int = BROWSER_WAIT_TIMEOUT_MS) -> bool:
    """
    Ждёт появления текста на странице (или внутри `selector`).

    Текст уезжает **аргументом**, а не подстановкой в код: кавычка, обратный слэш
    или скобка в ожидаемой строке иначе сломали бы сам селектор — а ждут обычно
    ровно то, что написал сайт, со всей его пунктуацией. По той же причине здесь не
    `:has-text()`: его строку тоже пришлось бы экранировать.

    Сравнение идёт по `innerText`, то есть по видимому тексту: скрытый шаблон в
    разметке (`display: none`) совпадением не считается, иначе ожидание
    заканчивалось бы на заготовке сообщения, которую страница ещё не показала.

    Raises:
        TimeoutError: текст не появился за отведённое время.
    """
    if not str(text or '').strip():
        raise ValueError('wait_text: нечего ждать — текст пуст')

    try:
        await page.wait_for_function(
            '([sel, needle]) => {'
            '  const root = sel ? document.querySelector(sel) : document.body;'
            '  return !!root && ((root.innerText || "").indexOf(needle) >= 0);'
            '}',
            arg=[selector, str(text)], timeout=timeout_ms)
    except Exception as e:
        raise TimeoutError(f'текст «{str(text)[:80]}» не появился за {timeout_ms} мс: '
                           f'{str(e).splitlines()[0] if str(e) else type(e).__name__}')

    return True

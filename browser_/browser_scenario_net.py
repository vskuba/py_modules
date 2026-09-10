"""Перехват сети на прогоне: чем страница разговаривает с сервером.

⚠ **Заведён, чтобы сценарий можно было заменить HTTP-запросом.** Сценарий умеет
нажать кнопку, но не умеет ответить «а куда она постит» — а именно это и нужно,
когда прогон хотят убрать: страница чата, отправка сообщения, добавление в
избранное сводятся к одному запросу, если этот запрос знать. Прогон стоит десятков
секунд живого браузера и вкладки в общем пуле; запрос — миллисекунд. Подставить к
такому запросу свой вход поможет `browser_cookie_header`.

⚠ **Состояние живёт на прогон, а не на страницу.** Сессию переиспользуют
(`keep_session`, живое окно человека), и слушатель, оставленный на странице, дожил
бы до следующего прогона: тот получил бы чужие записи, а список рос бы бесконечно.
Поэтому состояние заводит прогон и снимает слушателя в своём `finally`.
"""

from logging_.logging_ import logger_info

from browser_.browser_scenario import (BROWSER_SCENARIO_NET_BODY_LIMIT,
                                       BROWSER_SCENARIO_NET_HEADERS,
                                       BROWSER_SCENARIO_NET_LIMIT)


def browser_scenario_net_new() -> dict:
    """Пустое состояние перехвата — его держит у себя прогон."""
    return {'rows': [], 'filter': '', 'handler': None}


def browser_scenario_net_start(page, net: dict, url_filter: str = '') -> str:
    """
    Начинает писать запросы страницы. Возвращает строку для трассы шага.

    ⚠ **Повторный вызов — «смотри заново», а не «смотри вдвое»**: прежний слушатель
    снимается, список обнуляется. Иначе записи первого наблюдения попали бы во
    второй съём, и разобрать, что чему принадлежит, было бы нечем.
    """
    browser_scenario_net_stop(page, net)

    net['rows'] = []
    net['filter'] = str(url_filter or '').lower()

    def catch(request):
        """Одна запись о запросе. ⚠ Ошибку глотаем: слушатель живёт внутри
        Playwright, и исключение в нём уронило бы не шаг, а весь прогон."""
        try:
            if len(net['rows']) >= BROWSER_SCENARIO_NET_LIMIT:
                return
            if net['filter'] and net['filter'] not in request.url.lower():
                return

            net['rows'].append(_row(request))
        except Exception as e:
            logger_info(f'[browser] запрос не записался: {e}')

    net['handler'] = catch
    page.on('request', catch)

    return f'слежу за «{url_filter}»' if url_filter else 'слежу за всем'


def browser_scenario_net_stop(page, net: dict | None) -> None:
    """Снимает слушателя, если он стоял. Молча: слушатель — след прогона, и
    неудача его снятия не должна ронять ответ, который уже собран."""
    if not net or not net.get('handler'):
        return

    try:
        page.remove_listener('request', net['handler'])
    except Exception as e:
        logger_info(f'[browser] слушатель запросов не снялся: {e}')

    net['handler'] = None


def browser_scenario_net_dump(net: dict | None, url_filter: str = '') -> list:
    """
    Записанное — списком. `url_filter` отбирает поверх снятого.

    Поверх, а не вместо: следить можно широко, а разбирать узко — так один прогон
    отвечает и на «что вообще уходит», и на «а вот это куда».
    """
    rows = list((net or {}).get('rows') or [])
    if not url_filter:
        return rows

    needle = str(url_filter).lower()

    return [row for row in rows if needle in str(row.get('url', '')).lower()]


def _row(request) -> dict:
    """Запрос в вид, годный для того, чтобы его повторить своими силами.

    ⚠ **Только синхронные свойства.** Слушатель `page.on('request')` вызывается
    синхронно, и `await request.all_headers()` здесь недоступен — остаются
    `headers`, `post_data`, `method`, `url`. Их хватает: чтобы повторить запрос,
    нужны адрес, метод, тип тела и само тело.

    ⚠ **Заголовки — по закрытому списку** (`BROWSER_SCENARIO_NET_HEADERS`): в них
    живут `cookie` и `authorization`, то есть сама сессия. Уедь она в `extract`,
    чужая сессия оказалась бы в нашей базе открытым текстом.
    """
    headers = {}
    try:
        raw = request.headers or {}
        headers = {key: str(raw[key])[:200] for key in BROWSER_SCENARIO_NET_HEADERS
                   if key in raw}
    except Exception:
        pass

    # ⚠ Тело бывает недоступно (GET, загрузка файла) — это не ошибка, а отсутствие
    # тела. Пустая строка честнее исключения посреди слушателя.
    body = ''
    try:
        body = str(request.post_data or '')[:BROWSER_SCENARIO_NET_BODY_LIMIT]
    except Exception:
        body = ''

    kind = ''
    try:
        kind = str(request.resource_type or '')
    except Exception:
        kind = ''

    return {'method': str(request.method or ''), 'url': str(request.url or '')[:1000],
            'kind': kind, 'headers': headers, 'body': body}

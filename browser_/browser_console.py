"""Журнал самой страницы: её консоль, её исключения, её неудавшиеся запросы.

Отвечает на вопрос, на который трасса шага ответить не может. Шаг упал на
«элемент не найден» — и это правда, но не причина: элемента нет потому, что на
странице не отработал её собственный JavaScript. В трассе видно следствие, в
консоли — причина: `Uncaught TypeError`, 403 от своего же API, не загрузившийся
модуль. Без этого журнала починка сценария вырождается в перебор селекторов.

Пишем три разных потока, и путать их нельзя:

  * `console` — то, что страница напечатала сама (`console.log`, `warn`, `error`);
  * `pageerror` — исключение, которое никто не поймал. Самое ценное: после него
    страница обычно не дорисована, и любой шаг по ней упадёт;
  * `requestfailed` — запрос, который не состоялся. Отдельно от кода ответа: 500
    страница получила и как-то переварила, а оборванный запрос она не получила
    вовсе, и часто именно поэтому список пуст.

⚠ **Слушатели снимаются тем же, кто их ставил.** Сессию переиспользуют — живое
окно человека, следующий прогон, — и оставленный слушатель копил бы записи до
конца жизни страницы, отдавая их тому, кто про них не знает.
"""

import time

from logging_.logging_ import logger_info

from browser_.browser_pool import browser_pool_pages

# Сколько записей держим. Болтливая страница печатает в консоль на каждый кадр
# анимации; предел — чтобы журнал сессии, живущей неделями, не съел память
# контейнера. Первые записи важнее последних (причина сбоя обычно в начале), но
# выбрасывать приходится их: иначе новые не поместятся вовсе.
BROWSER_CONSOLE_LIMIT = 300

# Сколько знаков сообщения берём. Страница печатает в консоль объекты целиком, и
# один `console.log(response)` бывает на десятки килобайт.
BROWSER_CONSOLE_TEXT_LIMIT = 1000

# Какие типы сообщений консоли считаем достойными журнала. `debug` и `trace`
# отброшены: их печатают в цикле, и они вытесняют собой всё остальное.
BROWSER_CONSOLE_TYPES = ('log', 'info', 'warning', 'error', 'assert')


def browser_console_start(page, text_filter: str = '',
                          limit: int = BROWSER_CONSOLE_LIMIT) -> dict:
    """
    Начинает писать журнал одной страницы. Возвращает состояние — его и хранить.

    Состояние наружу, а не в модульный словарь, специально: журнал бывает нужен
    **на прогон сценария** (своя запись, гаснет с прогоном) и **на сессию** (живёт,
    пока человек смотрит окно). Один общий реестр заставил бы этих двух делить одни
    записи, и прогон уносил бы у окна его журнал.
    """
    state = {'rows': [], 'filter': str(text_filter or '').lower(),
             'limit': int(limit), 'handlers': [], 'started_at': time.time(),
             'dropped': 0}

    _watch(page, state)

    return state


def browser_console_session_start(entry: dict, text_filter: str = '',
                                  limit: int = BROWSER_CONSOLE_LIMIT) -> dict:
    """
    То же, но на всю сессию: все её страницы и те, что всплывут позже.

    Страниц у сессии больше одной — сайт открывает дочерние окна, — и ошибка
    случается как раз в том, которое всплыло: форма входа, окно оплаты, чат в
    отдельном окне. Журнал только первой страницы про них не знает ничего.

    Состояние кладётся в запись сессии (`entry['console']`), потому что гаснуть оно
    должно вместе с сессией: второй реестр пришлось бы чистить руками.
    """
    browser_console_session_stop(entry)

    state = {'rows': [], 'filter': str(text_filter or '').lower(),
             'limit': int(limit), 'handlers': [], 'started_at': time.time(),
             'dropped': 0}

    for page in browser_pool_pages(entry):
        _watch(page, state)

    # Всплывшее окно догоняем подпиской: ставить слушателей заранее некуда, а
    # `add_init_script` тут не поможет — журнал собирает Playwright снаружи
    # страницы, а не скрипт внутри неё.
    def on_page(page):
        _watch(page, state)

    context = entry['context']
    context.on('page', on_page)
    state['handlers'].append((context, 'page', on_page))

    entry['console'] = state
    logger_info(f"[browser] журнал консоли включён: {entry['session_id']}")

    return state


def browser_console_session_stop(entry: dict) -> list:
    """Гасит журнал сессии и отдаёт накопленное. Не велся — пустой список."""
    state = entry.pop('console', None)
    if state is None:
        return []

    browser_console_stop(state)

    return state['rows']


def browser_console_stop(state: dict | None) -> None:
    """Снимает всех слушателей состояния. Записи остаются — их ещё не забрали."""
    if not state:
        return

    for target, event, handler in state.get('handlers') or []:
        try:
            target.remove_listener(event, handler)
        except Exception as e:
            logger_info(f'[browser] журнал консоли: слушатель не снялся: {e}')

    state['handlers'] = []


def browser_console_rows(state: dict | None, text_filter: str = '',
                         kinds: str = '') -> list:
    """
    Накопленные записи. `text_filter` и `kinds` — отбор поверх снятого.

    Отбор именно поверх: писать можно широко, а разбирать узко — так один прогон
    отвечает и на «что вообще печатала страница», и на «а ошибки были?». Список
    видов приходит строкой через запятую (`error,pageerror`), потому что уезжает
    он полем шага сценария, где всё — строки.
    """
    rows = list((state or {}).get('rows') or [])

    needle = str(text_filter or '').lower()
    if needle:
        rows = [row for row in rows if needle in str(row.get('text', '')).lower()]

    wanted = {item.strip() for item in str(kinds or '').split(',') if item.strip()}
    if wanted:
        rows = [row for row in rows if row.get('kind') in wanted]

    return rows


def browser_console_info(state: dict | None) -> dict:
    """Сводка для списка сессий: идёт ли запись, сколько строк, сколько потеряно."""
    if not state:
        return {'live': False, 'rows': 0, 'dropped': 0, 'uptime_sec': 0}

    return {
        'live': bool(state.get('handlers')),
        'rows': len(state.get('rows') or []),
        'dropped': int(state.get('dropped') or 0),
        'uptime_sec': round(time.time() - float(state.get('started_at') or time.time())),
        'errors': len([row for row in state.get('rows') or []
                       if row.get('kind') in ('error', 'pageerror', 'requestfailed')]),
    }


def _watch(page, state: dict) -> None:
    """Ставит на страницу три слушателя и запоминает их для снятия."""
    def on_console(message):
        try:
            if message.type not in BROWSER_CONSOLE_TYPES:
                return
            _row_add(state, message.type, message.text, _location(message))
        except Exception as e:
            # ⚠ Исключение здесь уронило бы не журнал, а вызов Playwright, внутри
            # которого слушатель и работает: страница получила бы сбой из-за того,
            # что мы не смогли записать её же сообщение.
            logger_info(f'[browser] сообщение консоли не записалось: {e}')

    def on_pageerror(error):
        try:
            _row_add(state, 'pageerror', str(error), '')
        except Exception as e:
            logger_info(f'[browser] исключение страницы не записалось: {e}')

    def on_requestfailed(request):
        try:
            reason = ''
            failure = request.failure
            if failure:
                reason = str(failure)
            _row_add(state, 'requestfailed', f'{request.method} {reason}'.strip(),
                     str(request.url or ''))
        except Exception as e:
            logger_info(f'[browser] неудавшийся запрос не записался: {e}')

    for event, handler in (('console', on_console), ('pageerror', on_pageerror),
                           ('requestfailed', on_requestfailed)):
        page.on(event, handler)
        state['handlers'].append((page, event, handler))


def _row_add(state: dict, kind: str, text: str, where: str) -> None:
    """Одна запись журнала. Переполнение — не ошибка, а потеря самой старой строки."""
    text = str(text or '')[:BROWSER_CONSOLE_TEXT_LIMIT]
    if state['filter'] and state['filter'] not in text.lower():
        return

    rows = state['rows']
    if len(rows) >= state['limit']:
        rows.pop(0)
        state['dropped'] += 1

    rows.append({'kind': kind, 'text': text, 'where': str(where or '')[:500],
                 'at': time.strftime('%H:%M:%S')})


def _location(message) -> str:
    """`файл:строка` сообщения консоли. Нет — пустая строка, и это норма: у
    сообщения из встроенного скрипта места в файле нет."""
    try:
        location = message.location or {}
        url = str(location.get('url') or '')
        line = location.get('lineNumber')

        return f'{url}:{line}' if url and line is not None else url
    except Exception:
        return ''

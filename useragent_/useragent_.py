"""Профили браузеров: `User-Agent` и согласованные с ним заголовки.

Подставить случайную строку `User-Agent` мало — и это главное, что про неё
нужно знать. Сайт смотрит не на строку, а на **набор**: заявился Chrome 131 на
Windows, а `Sec-CH-UA-Platform` говорит «Linux», `Accept-Language` пуст, а
`Sec-Fetch-*` нет вовсе — это не человек. Такой гость заметнее, чем вовсе без
подмены: обычный `python-httpx/0.27` хотя бы честен.

Поэтому здесь не список строк, а **профили**: у каждого своя строка, своя
платформа, свои клиентские подсказки и свой язык. Берут профиль целиком.

## ⚠⚠ Версии стареют, и это не ошибка списка

Chrome выходит раз в месяц. Профиль двухлетней давности выглядит как человек,
не обновлявший браузер два года, — таких мало, и они заметны. Список правят
вместе с правкой кода; дата последнего обновления стоит в
`USERAGENT_UPDATED_AT`, и по ней видно, пора ли.

## ⚠ Доли, а не поровну

Chrome на Windows — две трети живого трафика, Safari на iPhone — заметная
часть, Firefox на Linux — редкость. Ровные доли по профилям дают набор, в
котором каждый десятый гость сидит на Linux, и это видно в любой аналитике.

## Чего здесь нет

Подмены отпечатка холста, шрифтов и WebGL. Это дело браузера, а не заголовков:
строка `User-Agent` говорит, кем гость назвался, а отпечаток — кем он оказался.
Модуль отвечает за первое.
"""

import random

# Когда список правили в последний раз. ⚠ Не украшение: по нему видно, что
# версии состарились (см. шапку). Правя список, правьте и дату.
USERAGENT_UPDATED_AT = '2026-10-15'

# Профили живых браузеров: строка, платформа, клиентские подсказки, язык и доля.
#
# ⚠⚠ `sec_ch_ua` обязан совпадать с версией в строке. Chrome шлёт обе, и сайт,
# сверяющий их, видит подделку мгновенно — а сверяют их все, кому это важно.
#
# ⚠ У Safari и Firefox клиентских подсказок НЕТ вовсе, и пустое значение здесь
# не недоделка: их шлёт только семейство Chromium. Приписать их Safari значит
# выдать себя тем самым, чем пытались прикрыться.
USERAGENT_PROFILES = (
    {
        'code': 'chrome-win',
        'title': 'Chrome 141 · Windows 11',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/141.0.0.0 Safari/537.36',
        'platform': 'Windows',
        'sec_ch_ua': '"Chromium";v="141", "Not=A?Brand";v="24", '
                     '"Google Chrome";v="141"',
        'mobile': False,
        'language': 'uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6',
        'weight': 34,
    },
    {
        'code': 'chrome-android',
        'title': 'Chrome 141 · Android 14',
        'user_agent': 'Mozilla/5.0 (Linux; Android 14; SM-S918B) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/141.0.0.0 Mobile Safari/537.36',
        'platform': 'Android',
        'sec_ch_ua': '"Chromium";v="141", "Not=A?Brand";v="24", '
                     '"Google Chrome";v="141"',
        'mobile': True,
        'language': 'uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6',
        'weight': 26,
    },
    {
        'code': 'safari-iphone',
        'title': 'Safari 18 · iPhone (iOS 18)',
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) '
                      'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                      'Version/18.1 Mobile/15E148 Safari/604.1',
        'platform': '',
        'sec_ch_ua': '',
        'mobile': True,
        'language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
        'weight': 16,
    },
    {
        'code': 'chrome-mac',
        'title': 'Chrome 141 · macOS',
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/141.0.0.0 Safari/537.36',
        'platform': 'macOS',
        'sec_ch_ua': '"Chromium";v="141", "Not=A?Brand";v="24", '
                     '"Google Chrome";v="141"',
        'mobile': False,
        'language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
        'weight': 8,
    },
    {
        'code': 'edge-win',
        'title': 'Edge 141 · Windows 11',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0',
        'platform': 'Windows',
        'sec_ch_ua': '"Chromium";v="141", "Not=A?Brand";v="24", '
                     '"Microsoft Edge";v="141"',
        'mobile': False,
        'language': 'uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6',
        'weight': 7,
    },
    {
        'code': 'safari-mac',
        'title': 'Safari 18 · macOS',
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                      'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                      'Version/18.1 Safari/605.1.15',
        'platform': '',
        'sec_ch_ua': '',
        'mobile': False,
        'language': 'uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7',
        'weight': 5,
    },
    {
        'code': 'firefox-win',
        'title': 'Firefox 133 · Windows 11',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) '
                      'Gecko/20100101 Firefox/133.0',
        'platform': '',
        'sec_ch_ua': '',
        'mobile': False,
        'language': 'uk-UA,uk;q=0.8,ru;q=0.6,en-US;q=0.4,en;q=0.2',
        'weight': 4,
    },
)

# Заголовки, которые шлёт всякий живой браузер при переходе по ссылке.
#
# ⚠⚠ `Sec-Fetch-*` — не мелочь. Их шлёт сам браузер, подделать забывают, и
# запрос без них при современном `User-Agent` выглядит ровно тем, чем является.
#
# ⚠⚠ Набор снят с НАВИГАЦИИ, и живому браузеру целиком не годится: там эти
# заголовки вешаются на контекст, то есть и на стили со скриптами, а подзапрос
# с заданным вручную `Sec-Fetch-*` Chromium не отправляет вовсе. Пул браузера
# фильтрует набор сам (`browser_pool._pool_headers`) — звать так же, как
# раньше, верно; разбор — `docs/browser_control.md`, §2.1.
#
# ⚠⚠ `Accept-Encoding` здесь НЕТ намеренно, и это не забывчивость. Его ставит
# сам клиент — тем, что умеет разжать. Объяви мы `br`, а библиотека без brotli
# — и сервер пришлёт сжатое тело, которое не развернётся: страница приедет
# двоичным мусором, разбор найдёт ноль товаров, и выглядеть это будет как
# «магазин опустел». Поймано ровно так: после подстановки заголовков поиск,
# находивший пятнадцать товаров, стал находить ноль.
USERAGENT_COMMON_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
              'image/avif,image/webp,image/apng,*/*;q=0.8,'
              'application/signed-exchange;v=b3;q=0.7',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}


def useragent_list() -> list:
    """Все профили справочника — как они есть.

    ⚠ Копии, а не сами словари: зовущий складывает их в базу и правит под себя,
    и правка не должна менять справочник для всего процесса.
    """
    return [dict(row) for row in USERAGENT_PROFILES]


def useragent_find(code: str) -> dict:
    """Профиль по коду; нет такого — пустой словарь.

    Пустой словарь, а не отказ: код приезжает из базы, где строка могла
    остаться от профиля, убранного из справочника. Это «не знаем, кем
    представиться», а не повод ронять заказ.
    """
    for row in USERAGENT_PROFILES:
        if row['code'] == str(code or ''):
            return dict(row)

    return {}


def useragent_pick(codes=(), rand: random.Random = None) -> dict:
    """Профиль по долям; `codes` сужает выбор до перечисленных.

    Args:
        codes: коды, из которых выбирать. Пусто — из всех.
        rand: источник случайности; свой нужен тестам.

    ⚠ Доли, а не поровну: Chrome на Windows — две трети живого трафика, Firefox
    на Linux — редкость. Ровный выбор даёт набор, где каждый десятый гость
    сидит на редкости, и это видно в любой аналитике.
    """
    rand = rand or random
    pool = [row for row in USERAGENT_PROFILES
            if not codes or row['code'] in set(codes)]
    if not pool:
        return {}

    return dict(rand.choices(pool, weights=[row['weight'] for row in pool],
                             k=1)[0])


def useragent_headers(profile: dict, referer: str = '') -> dict:
    """Заголовки запроса, согласованные с профилем.

    Args:
        profile: словарь профиля (`useragent_pick`, `useragent_find`).
        referer: откуда «пришли». Пусто — заход прямой, и `Sec-Fetch-Site`
            остаётся `none`; задан — браузер прислал бы `same-origin`.

    ⚠⚠ Клиентские подсказки ставятся **только** тем, кто их шлёт. У Safari и
    Firefox их нет вовсе, и приписать их значит выдать себя тем самым, чем
    пытались прикрыться.

    ⚠ Пустой профиль — пустые заголовки, а не умолчание: «не знаем, кем
    представиться» честнее, чем представиться наугад.
    """
    if not profile or not profile.get('user_agent'):
        return {}

    headers = dict(USERAGENT_COMMON_HEADERS)
    headers['User-Agent'] = profile['user_agent']
    headers['Accept-Language'] = profile.get('language', 'en-US,en;q=0.9')

    if profile.get('sec_ch_ua'):
        headers['Sec-CH-UA'] = profile['sec_ch_ua']
        headers['Sec-CH-UA-Mobile'] = '?1' if profile.get('mobile') else '?0'
        if profile.get('platform'):
            headers['Sec-CH-UA-Platform'] = f'"{profile["platform"]}"'

    if referer:
        headers['Referer'] = referer
        # Переход внутри сайта — не прямой заход, и браузер говорит об этом.
        headers['Sec-Fetch-Site'] = 'same-origin'

    return headers

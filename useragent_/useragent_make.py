"""Генератор отпечатка браузера: полный набор заголовков, не повторяющийся дважды.

Семь профилей на всю установку — это семь одинаковых гостей. Магазин, которому
полсотни заказов в сутки приходят с одной из семи строк, видит их как семь
человек, оформивших по десять заказов каждый. Генератор даёт **свой набор
каждой карточке**: версия браузера, модель устройства, порядок языков, размер
окна — всё в тех пределах, в каких это различается у живых людей.

## ⚠⚠ Различается не всё, и в этом вся работа

Случайный набор даёт невозможного гостя: Chrome на iPhone, Safari с
`Sec-CH-UA`, мобильный `User-Agent` при окне 1920×1080. Такой отпечаток
уникален — и мгновенно опознаётся как поддельный, то есть хуже повторяющегося.

Поэтому генератор варьирует **только то, что варьируется у людей**:

| Варьируется | Не варьируется никогда |
|-------------|------------------------|
| мажорная версия браузера | связь «платформа ↔ строка ↔ подсказки» |
| версия ОС, модель устройства | наличие `Sec-CH-UA` у не-Chromium |
| порядок и веса языков | мобильность при настольном окне |
| размер окна (из живых) | минорная часть версии Chrome (она заморожена) |

## ⚠⚠ Версия привязана к настоящему движку

`USERAGENT_MAKE_CHROME` — окно версий вокруг Chromium, который стоит в образе
браузера. Заявить 155, когда движок 151, — значит соврать о себе тому, кто
сверит строку с поведением: набор поддерживаемых возможностей у версий разный,
и проверить это со страницы ничего не стоит.

⚠ Правя образ браузера, правьте и это окно. Расхождение не ломает ничего
видимого — оно просто делает отпечаток заметным.

## ⚠ Отпечаток считается по набору, а не по строке

`fingerprint` — хеш всего набора: строки, языка, окна, пояса. Две карточки с
одной строкой, но разным окном — разные гости; с одинаковым всем — один, и
второй такой заводить незачем.
"""

import hashlib
import json
import random

from useragent_.useragent_ import USERAGENT_COMMON_HEADERS

# Мажорные версии Chrome, которыми представляемся, и их доли. ⚠⚠ Окно вокруг
# Chromium в образе браузера (см. шапку): люди обновляются не мгновенно, но и
# не отстают на год. Свежая версия — у большинства, предыдущие — у хвоста.
USERAGENT_MAKE_CHROME = ((155, 55), (154, 25), (153, 12), (152, 8))

# То же для Firefox: он обновляется своим чередом и отстаёт от Chrome нумерацией.
USERAGENT_MAKE_FIREFOX = ((147, 55), (146, 28), (145, 17))

# Версии Safari и iOS — парой: Safari на iPhone не бывает новее системы.
USERAGENT_MAKE_IOS = (
    ('18_6', '18.6', 40), ('18_5', '18.5', 22), ('18_1', '18.1', 14),
    ('17_7', '17.6', 14), ('26_0', '26.0', 10),
)

# Модели Android, какие водятся в Украине. ⚠ С версией системы парой: Android
# 15 на модели трёхлетней давности встречается, а Android 10 на новой — нет.
USERAGENT_MAKE_ANDROID = (
    ('SM-S928B', '15', 8), ('SM-S918B', '14', 10), ('SM-A556E', '15', 9),
    ('SM-A546E', '14', 8), ('SM-A356E', '14', 7), ('SM-A146P', '14', 6),
    ('Pixel 9', '15', 5), ('Pixel 8', '15', 5), ('Pixel 7', '14', 4),
    ('2201116SG', '13', 5), ('23021RAA2Y', '14', 5), ('2311DRK48G', '14', 5),
    ('CPH2451', '14', 4), ('CPH2565', '15', 4), ('RMX3771', '14', 4),
    ('V2339', '14', 3), ('M2101K6G', '13', 3), ('SM-A245F', '14', 5),
)

# Версии Windows в строке. ⚠ `NT 10.0` и у Windows 10, и у 11: Microsoft не
# стала менять номер, и отличить их по строке нельзя вовсе. Вариант один.
USERAGENT_MAKE_WINDOWS = 'Windows NT 10.0; Win64; x64'

# Версии macOS в строке Chrome. ⚠ `10_15_7` заморожено Apple и Google: новые
# системы шлют его же, и «правдивая» 14_6 выдала бы подделку.
USERAGENT_MAKE_MAC = '10_15_7'

# Наборы языков, какие встречаются у украинского покупателя. ⚠⚠ Не случайная
# перестановка: порядок и веса у браузера строго убывающие, а набор отражает
# реальный выбор человека в настройках.
USERAGENT_MAKE_LANGUAGES = (
    ('uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6', 30),
    ('uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7', 22),
    ('uk,ru;q=0.9,en-US;q=0.8,en;q=0.7', 14),
    ('ru-UA,ru;q=0.9,uk;q=0.8,en-US;q=0.7,en;q=0.6', 12),
    ('uk-UA,uk;q=0.8,ru;q=0.6,en-US;q=0.4,en;q=0.2', 10),
    ('ru,uk;q=0.9,en;q=0.8', 7),
    ('en-US,en;q=0.9,uk;q=0.8', 5),
)

# Размеры окна настольного браузера — из живых разрешений за вычетом панелей.
USERAGENT_MAKE_DESKTOP_VIEW = (
    ((1536, 730), 18), ((1920, 955), 16), ((1366, 625), 14), ((1440, 790), 10),
    ((1280, 585), 9), ((1600, 820), 8), ((2560, 1300), 7), ((1680, 870), 6),
    ((1920, 915), 6), ((1512, 790), 6),
)

# То же для телефонов: ширина в CSS-пикселях, а не в точках матрицы.
USERAGENT_MAKE_MOBILE_VIEW = (
    ((360, 800), 22), ((393, 852), 16), ((412, 915), 14), ((390, 844), 12),
    ((414, 896), 9), ((375, 812), 8), ((430, 932), 7), ((360, 780), 6),
    ((428, 926), 6),
)

# Пояса. ⚠ Киев почти всегда: покупатель украинского магазина сидит в Украине,
# и пояс, не совпадающий со страной выхода, — несоответствие, а не разнообразие.
USERAGENT_MAKE_TIMEZONES = (('Europe/Kyiv', 92), ('Europe/Warsaw', 5),
                            ('Europe/Berlin', 3))

# Семьи браузеров и их доли в живом трафике.
USERAGENT_MAKE_FAMILIES = (
    ('chrome-win', 32), ('chrome-android', 26), ('safari-iphone', 16),
    ('chrome-mac', 8), ('edge-win', 7), ('safari-mac', 5), ('firefox-win', 4),
    ('opera-win', 2),
)


def useragent_make(family: str = '', rand: random.Random = None) -> dict:
    """Собрать отпечаток браузера: строка, заголовки, окно, язык, пояс.

    Args:
        family: какую семью собирать (`chrome-win`, `safari-iphone`, …). Пусто
            — выбирается по долям живого трафика.
        rand: источник случайности; свой нужен тестам.

    Returns:
        `{'family', 'title', 'user_agent', 'platform', 'mobile', 'language',
          'viewport': {'width', 'height'}, 'timezone', 'headers',
          'fingerprint'}`.

    ⚠⚠ Возвращается **согласованный** набор, а не случайный. Chrome на iPhone,
    Safari с `Sec-CH-UA`, мобильная строка при окне 1920 — уникальны и
    мгновенно опознаются как подделка (см. шапку модуля).
    """
    rand = rand or random
    family = family or _pick(USERAGENT_MAKE_FAMILIES, rand)
    build = _BUILDERS.get(family)
    if not build:
        return {}

    shape = build(rand)
    shape['family'] = family
    shape['language'] = _pick(USERAGENT_MAKE_LANGUAGES, rand)
    shape['timezone'] = _pick(USERAGENT_MAKE_TIMEZONES, rand)
    width, height = _pick(
        USERAGENT_MAKE_MOBILE_VIEW if shape['mobile']
        else USERAGENT_MAKE_DESKTOP_VIEW, rand)
    shape['viewport'] = {'width': width, 'height': height}
    shape['headers'] = useragent_make_headers(shape)
    shape['fingerprint'] = useragent_make_fingerprint(shape)

    return shape


def useragent_make_headers(shape: dict) -> dict:
    """Заголовки набора — те же правила, что у готовых профилей.

    ⚠ Клиентские подсказки ставятся только семейству Chromium: у Safari и
    Firefox их нет вовсе, и приписать их значит выдать себя тем самым, чем
    прикрывались (`useragent.md`, §2).
    """
    headers = dict(USERAGENT_COMMON_HEADERS)
    headers['User-Agent'] = shape['user_agent']
    headers['Accept-Language'] = shape['language']

    if shape.get('sec_ch_ua'):
        headers['Sec-CH-UA'] = shape['sec_ch_ua']
        headers['Sec-CH-UA-Mobile'] = '?1' if shape['mobile'] else '?0'
        if shape.get('platform'):
            headers['Sec-CH-UA-Platform'] = f'"{shape["platform"]}"'

    return headers


def useragent_make_fingerprint(shape: dict) -> str:
    """Отпечаток набора — чем одна карточка отличается от другой.

    ⚠ Считается по **набору**, а не по строке: две карточки с одной строкой,
    но разным окном и языком — разные гости. Хеш нужен затем, чтобы уникальность
    проверяла база одним ключом, а не перебором полей.
    """
    parts = [shape.get('user_agent', ''), shape.get('language', ''),
             shape.get('timezone', ''),
             str(shape.get('viewport', {}).get('width', '')),
             str(shape.get('viewport', {}).get('height', ''))]

    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


def useragent_make_many(count: int, taken=(), tries: int = 8,
                        rand: random.Random = None) -> list:
    """Пачка наборов без повторов; `taken` — отпечатки, которые уже есть.

    ⚠ Повтор не ошибка, а исчерпание: сочетаний конечное число, и на сотнях
    наборов они начинают совпадать. Поэтому попытка повторяется `tries` раз, а
    потом набор просто пропускается — вернётся меньше, чем просили, и зовущий
    увидит это по длине списка.
    """
    rand = rand or random
    busy = set(taken or ())
    made = []

    for _ in range(max(0, int(count))):
        for _ in range(max(1, int(tries))):
            shape = useragent_make(rand=rand)
            if shape and shape['fingerprint'] not in busy:
                busy.add(shape['fingerprint'])
                made.append(shape)
                break

    return made


# ── детали реализации ──────────────────────────────────────────────────────

def _pick(weighted, rand: random.Random):
    """Значение из пар «что, доля»; у троек первые два поля — значение."""
    rows = list(weighted)
    weights = [row[-1] for row in rows]
    got = rand.choices(rows, weights=weights, k=1)[0]

    return got[0] if len(got) == 2 else tuple(got[:-1])


def _chrome_hints(major: int, brand: str = 'Google Chrome') -> str:
    """`Sec-CH-UA` для версии. ⚠ Порядок брендов у Chrome свой и постоянный."""
    return (f'"Chromium";v="{major}", "Not=A?Brand";v="24", '
            f'"{brand}";v="{major}"')


def _build_chrome_win(rand) -> dict:
    major = _pick(USERAGENT_MAKE_CHROME, rand)

    return {
        'title': f'Chrome {major} · Windows',
        'user_agent': f'Mozilla/5.0 ({USERAGENT_MAKE_WINDOWS}) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      f'Chrome/{major}.0.0.0 Safari/537.36',
        'platform': 'Windows', 'mobile': False,
        'sec_ch_ua': _chrome_hints(major),
    }


def _build_chrome_mac(rand) -> dict:
    major = _pick(USERAGENT_MAKE_CHROME, rand)

    return {
        'title': f'Chrome {major} · macOS',
        'user_agent': f'Mozilla/5.0 (Macintosh; Intel Mac OS X {USERAGENT_MAKE_MAC}) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      f'Chrome/{major}.0.0.0 Safari/537.36',
        'platform': 'macOS', 'mobile': False,
        'sec_ch_ua': _chrome_hints(major),
    }


def _build_chrome_android(rand) -> dict:
    major = _pick(USERAGENT_MAKE_CHROME, rand)
    model, android = _pick(USERAGENT_MAKE_ANDROID, rand)

    return {
        'title': f'Chrome {major} · Android {android} ({model})',
        'user_agent': f'Mozilla/5.0 (Linux; Android {android}; {model}) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      f'Chrome/{major}.0.0.0 Mobile Safari/537.36',
        'platform': 'Android', 'mobile': True,
        'sec_ch_ua': _chrome_hints(major),
    }


def _build_edge_win(rand) -> dict:
    major = _pick(USERAGENT_MAKE_CHROME, rand)

    return {
        'title': f'Edge {major} · Windows',
        'user_agent': f'Mozilla/5.0 ({USERAGENT_MAKE_WINDOWS}) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      f'Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0',
        'platform': 'Windows', 'mobile': False,
        'sec_ch_ua': _chrome_hints(major, 'Microsoft Edge'),
    }


def _build_opera_win(rand) -> dict:
    major = _pick(USERAGENT_MAKE_CHROME, rand)
    # ⚠ Своя нумерация: Opera отстаёт от Chromium примерно на сорок версий.
    opera = major - 40

    return {
        'title': f'Opera {opera} · Windows',
        'user_agent': f'Mozilla/5.0 ({USERAGENT_MAKE_WINDOWS}) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      f'Chrome/{major}.0.0.0 Safari/537.36 OPR/{opera}.0.0.0',
        'platform': 'Windows', 'mobile': False,
        'sec_ch_ua': _chrome_hints(major, 'Opera'),
    }


def _build_safari_iphone(rand) -> dict:
    ios, safari = _pick(USERAGENT_MAKE_IOS, rand)

    return {
        'title': f'Safari {safari} · iPhone (iOS {ios.replace("_", ".")})',
        'user_agent': f'Mozilla/5.0 (iPhone; CPU iPhone OS {ios} like Mac OS X) '
                      'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                      f'Version/{safari} Mobile/15E148 Safari/604.1',
        # ⚠ Платформы и подсказок нет: Safari их не шлёт вовсе.
        'platform': '', 'mobile': True, 'sec_ch_ua': '',
    }


def _build_safari_mac(rand) -> dict:
    _, safari = _pick(USERAGENT_MAKE_IOS, rand)

    return {
        'title': f'Safari {safari} · macOS',
        'user_agent': f'Mozilla/5.0 (Macintosh; Intel Mac OS X {USERAGENT_MAKE_MAC}) '
                      'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                      f'Version/{safari} Safari/605.1.15',
        'platform': '', 'mobile': False, 'sec_ch_ua': '',
    }


def _build_firefox_win(rand) -> dict:
    major = _pick(USERAGENT_MAKE_FIREFOX, rand)

    return {
        'title': f'Firefox {major} · Windows',
        'user_agent': f'Mozilla/5.0 ({USERAGENT_MAKE_WINDOWS}; rv:{major}.0) '
                      f'Gecko/20100101 Firefox/{major}.0',
        'platform': '', 'mobile': False, 'sec_ch_ua': '',
    }


# Семья → как её собрать. ⚠ Явным словарём, а не поиском функции по имени:
# состав семей — часть договора, и незнакомое имя обязано давать пустой ответ,
# а не падение где-то внутри.
_BUILDERS = {
    'chrome-win': _build_chrome_win,
    'chrome-mac': _build_chrome_mac,
    'chrome-android': _build_chrome_android,
    'edge-win': _build_edge_win,
    'opera-win': _build_opera_win,
    'safari-iphone': _build_safari_iphone,
    'safari-mac': _build_safari_mac,
    'firefox-win': _build_firefox_win,
}


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog='python -m useragent_.useragent_make',
        description='Отпечатки браузера: строка, заголовки, окно, язык, пояс.')
    parser.add_argument('count', nargs='?', type=int, default=5)
    parser.add_argument('--family', default='', help='только эта семья')
    parser.add_argument('--seed', type=int, default=0, help='повторяемый набор')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    rand = random.Random(args.seed) if args.seed else None
    rows = ([useragent_make(args.family, rand) for _ in range(args.count)]
            if args.family else useragent_make_many(args.count, rand=rand))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            view = row['viewport']
            print(f'{row["title"]:44} {view["width"]}×{view["height"]:<5} '
                  f'{row["timezone"]:14} {row["fingerprint"][:8]}')
            print(f'   {row["user_agent"]}')

    return 0


if __name__ == '__main__':
    raise SystemExit(_main())

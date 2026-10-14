"""Живая сессия браузера со стороны клиента: снять кадр, закрыть.

Сценарий умеет ходить по странице, но не умеет показать, что на ней вышло.
Между «прогон кончился» и «человек увидел» лежит кадр, а снимает его контейнер
по номеру сессии — той самой, которую прогон оставил открытой
(`keep_session=True`).

## ⚠⚠ Открытая сессия — это вкладка Chromium, и её закрывают

Брошенная живёт до перезапуска контейнера. Два десятка таких съедают его
память, и выглядит это как «браузер вдруг стал медленным», а не как забытый
`close`. Поэтому у всякого `keep_session=True` обязана быть пара.

## ⚠ Кадр приходит base64, и это осознанно

Контейнер отдаёт картинку строкой в JSON, а не телом ответа: тем же вызовом
приходят и ошибки, и признак пустой страницы. Разбирать два вида ответа ради
экономии трети объёма дороже, чем эту треть заплатить.
"""

import base64

from browser_.browser_upstream import browser_upstream_request

# Сколько ждём кадр. Снимок большой страницы занимает секунды, а не миллисекунды:
# контейнер ждёт отрисовки, иначе кадр выходит с недогруженными картинками.
BROWSER_SESSION_SHOT_TIMEOUT = 60.0


async def browser_session_shot(session_id: str, selector: str = '',
                               full_page: bool = False) -> bytes:
    """Кадр открытой сессии — байтами PNG; пусто, если снять не вышло.

    Args:
        session_id: номер сессии, оставленной прогоном.
        selector: снять только этот элемент; пусто — видимую часть страницы.
        full_page: снять страницу целиком, включая то, что ниже экрана.

    ⚠ Пустые байты, а не исключение: кадр — это свидетельство, и его
    отсутствие не повод ронять работу, ради которой он снимался. Зовущий
    решает сам, беда это или нет.
    """
    if not str(session_id or '').strip():
        return b''

    try:
        answer = await browser_upstream_request(
            'POST', f'/browser/session/{session_id}/capture',
            {'selector': selector, 'full_page': bool(full_page)},
            timeout=BROWSER_SESSION_SHOT_TIMEOUT)
    except Exception:
        return b''

    image = str(answer.get('image') or '')
    if not image:
        return b''

    try:
        # Строка приходит и как `data:image/png;base64,…`, и голым base64 —
        # зависит от версии контейнера. Берём хвост после запятой в обоих
        # случаях: у голой строки запятой нет, и `split` вернёт её целиком.
        return base64.b64decode(image.split(',')[-1])
    except (ValueError, TypeError):
        return b''


async def browser_session_close(session_id: str) -> None:
    """Закрыть сессию; отказ глотается.

    ⚠ Молча: сессия могла закрыться сама по таймауту контейнера, и жаловаться
    на это значит шуметь в журнале о том, что уже в порядке. А вот **не**
    позвать эту функцию — беда настоящая (см. шапку).
    """
    if not str(session_id or '').strip():
        return

    try:
        await browser_upstream_request(
            'DELETE', f'/browser/session/{session_id}', timeout=30)
    except Exception:
        pass

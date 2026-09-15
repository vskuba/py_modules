"""Запрет живого HTTP в тестах: один сторож на все проекты.

## Зачем

Сюите нельзя ходить наружу — ни к соседнему проекту, ни к чужому сайту. Причин
три, и каждая проверена делом:

* **тест против чужой системы проверяет не нас.** Он краснеет от чужого
  перезапуска, от занятого пула, от переименованной записи — и молчит про то,
  ради чего написан;
* **он пишет в чужую систему.** Прогон занимает вкладку в общем пуле; вход в API
  чужого сайта — действие под живой учёткой, а частые заходы там считают
  подозрением и отвечают блокировкой;
* **важного им не проверить.** Занятый браузер, протухший токен, оборванная
  связь — ждать этого в нужную секунду нельзя, а с заглушкой это строка в тесте.

Обещание тут не работает: заглушку забывают, а **забытая заглушка не видна** —
тест зеленеет, просто идёт к чужой системе и делает там работу.

## ⚠⚠ Почему на HTTP, а не на сокете

Сокетный сторож — первое, что приходит в голову, и он **не ловит**. Проверено
живьём в DatingAi: сюита четыре раза за прогон входила на живой сайт знакомств под
живыми учётками, а сторож на `socket.connect` этого не видел. Две причины, и обе
здесь учтены:

**Сокет — слишком низко.** Туда ведут разные пути (httpx, вебсокеты, свои
клиенты), и каждый новый надо вспомнить. Транспорт httpx — одна дверь на все
клиенты процесса.

**Сторож должен стоять на весь прогон, а не на тест.** Часть работы уходит в
фоновые задачи, которых тест не ждёт (ручка, отвечающая сразу и снимающая данные
позже). Такая задача доживает до следующего теста или до конца прогона — когда
сторож, снятый вместе со своим тестом, уже не стоит.

## Как пользоваться

В `conftest.py` проекта, **один раз на прогон**:

    from http_.http_mock import http_mock_guard

    @pytest.fixture(scope='session', autouse=True)
    def no_outside_http():
        with http_mock_guard(allow=[('localhost', 8021), ('127.0.0.1', 8021)]):
            yield

В `allow` — только своя установка: адрес своей же панели, если сюита ходит в неё
по HTTP. Всё прочее обязано идти через заглушку.

⚠ **Заглушки при этом никуда не деваются.** Сторож говорит «сюда нельзя», а чем
ответить вместо живой системы — дело проекта: `httpx.MockTransport` внутри модуля
двери. Сторож лишь не даёт забыть.
"""

import httpx

# Что разрешено, если проект ничего не назвал. Пусто: «ничего наружу» — верное
# умолчание, а свой адрес проект знает сам.
HTTP_MOCK_ALLOW_DEFAULT = ()


class HttpMockError(AssertionError):
    """Тест полез наружу. ⚠ Наследник `AssertionError` намеренно.

    Так отказ читается как провал проверки, а не как поломка окружения: в отчёте
    pytest он встаёт рядом с обычными `assert`, и первым делом человек смотрит на
    тест, а не на сеть.
    """


def http_mock_allowed(host: str, port: int, allow) -> bool:
    """Свой ли это адрес.

    Args:
        host: куда стучимся.
        port: и в какой порт.
        allow: разрешённые пары `(хост, порт)`. Порт `0` — «любой у этого хоста».

    Returns:
        bool: `True` — можно.
    """
    host = str(host or '')
    port = int(port or 0)

    for one_host, one_port in allow or ():
        if str(one_host) == host and int(one_port or 0) in (0, port):
            return True

    return False


def http_mock_guard_start(allow=HTTP_MOCK_ALLOW_DEFAULT):
    """Ставит запрет на живой HTTP. Возвращает функцию, которая его снимет.

    Подменяется **транспорт**, а не клиент: клиентов в проекте бывает полдюжины
    (свой у каждой двери), и заглушка на каждом означала бы, что забытый шестой
    ходит наружу молча.

    ⚠ Вебсокеты httpx не видит вовсе — у них свой стек, и без отдельной подмены
    «запрет на HTTP» оставлял бы открытой дверь, через которую уходит живое
    сообщение живому человеку. Подменяем, если библиотека в проекте есть.
    """
    real_async = httpx.AsyncHTTPTransport.handle_async_request
    real_sync = httpx.HTTPTransport.handle_request

    def _check(url, who: str):
        port = getattr(url, 'port', None) or (443 if str(getattr(url, 'scheme', '')) in
                                              ('https', 'wss') else 80)
        if http_mock_allowed(getattr(url, 'host', ''), port, allow):
            return

        raise HttpMockError(
            f'тест полез наружу по HTTP ({who}): {url}. Живых запросов в тестах не '
            f'бывает — внешняя дверь обязана быть подменена заглушкой '
            f'(`httpx.MockTransport` внутри модуля двери).\n'
            f'⚠ Если это фоновая задача, которую тест не ждёт, — её надо либо '
            f'дождаться и подменить, либо не заводить в тесте вовсе.')

    async def _async(self, request):
        _check(request.url, 'httpx')

        return await real_async(self, request)

    def _sync(self, request):
        _check(request.url, 'httpx')

        return real_sync(self, request)

    httpx.AsyncHTTPTransport.handle_async_request = _async
    httpx.HTTPTransport.handle_request = _sync

    websockets, real_ws = _websockets()
    if websockets is not None:
        def _ws(uri, *args, **kwargs):
            _check(httpx.URL(str(uri)), 'websockets')

            return real_ws(uri, *args, **kwargs)

        websockets.connect = _ws

    def stop():
        httpx.AsyncHTTPTransport.handle_async_request = real_async
        httpx.HTTPTransport.handle_request = real_sync
        if websockets is not None:
            websockets.connect = real_ws

    return stop


class http_mock_guard:  # noqa: N801 — контекстный менеджер, имя по namespace
    """Тот же запрет менеджером контекста — так его ставят в фикстуре.

        with http_mock_guard(allow=[('localhost', 8021)]):
            yield
    """

    def __init__(self, allow=HTTP_MOCK_ALLOW_DEFAULT):
        self._allow = allow
        self._stop = None

    def __enter__(self):
        self._stop = http_mock_guard_start(self._allow)

        return self

    def __exit__(self, *args):
        if self._stop:
            self._stop()

        return False


# ── Приватное ────────────────────────────────────────────────────────────────

def _websockets():
    """Библиотека вебсокетов и её настоящий `connect`. Нет её — `(None, None)`.

    Лениво и молча: `websockets` нужен не каждому проекту, и требовать его ради
    сторожа значило бы тянуть зависимость туда, где вебсокетов нет вовсе.
    """
    try:
        import websockets
    except ImportError:
        return None, None

    return websockets, websockets.connect

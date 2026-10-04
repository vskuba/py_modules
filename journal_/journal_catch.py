"""Застава на корневом логгере: всё, что пишется как ошибка, попадает в журнал.

Журнал можно наполнять вызовами по месту — «поймал исключение, записал». Так его
наполняют ровно до первой забытой ветки: пишущий код занят своей работой, а
строчку про журнал в нём надо **помнить**. Напоминание, зависящее от памяти
того, кому оно адресовано, не работает.

Поэтому источник один и внешний: обработчик, подвешенный к корневому логгеру на
уровне ERROR. Любой `logger_exception`, `logging.error`, `logging.exception` и
необработанное исключение asyncio-задачи (его печатает сам asyncio через свой
логгер) оставляют строку в журнале **без единой строчки про журнал в коде**.

## ⚠ Ставится один раз на процесс, и его надо поставить

Обработчик не появляется сам: `logging_init()` про таблицы проекта не знает.
Поэтому каждая точка входа зовёт `journal_catch_install` рядом со своим
`logging_init()`: сервер, воркеры, кронджобы. Забытая точка входа не ломается —
она просто молчит в журнале.

## ⚠⚠ Не все логгеры доходят до корня

Uvicorn настраивает свои (`uvicorn.error`) с `propagate = False`: его
собственные ошибки до корневого обработчика не доходят вовсе. Поэтому установка
проходит по списку оторванных и подвешивается к каждому. Тем, кто до корня
доходит, второй обработчик не вешается: запись задвоилась бы — и стала бы двумя
повторами на одно падение.

## ⚠⚠ Защита от круга обязательна

Запись в базу сама зовёт код, который может пожаловаться в тот же логгер. Без
флага потока это уходит в бесконечную рекурсию, а не в журнал.

## ⚠ Куда класть этот обработчик нельзя

В процесс без базы. Если у контейнера нет доступа к базе журнала, застава
молча копила бы неудачи записи: ставить её туда не надо вовсе.
"""

import logging
import threading
import traceback as traceback_module

# Уровень, с которого пишем. Ниже ERROR — не ошибки: WARNING обычно означает
# «странно, но работаем», и журнал бы им затопило.
JOURNAL_CATCH_LEVEL = logging.ERROR

# Логгеры, которые бывают оторваны от корня своими настройками. Подвешиваемся к
# ним, только если `propagate` действительно выключен — иначе задвоение.
JOURNAL_CATCH_LOGGERS = ('uvicorn.error', 'rq.worker')

# Приметы чужого кода в пути файла. По ним из трассировки выбирается последний
# **свой** кадр (`_frame`).
JOURNAL_CATCH_FOREIGN = ('/site-packages/', '/dist-packages/', '/lib/python')

# Примета самого общего слоя. ⚠ Он тоже «чужой» для выбора кадра, хотя код наш:
# почти весь слой — тонкие обёртки, и на ошибке запроса кадр остановился бы на
# `mysql_.py:111 execute`, одинаковом у всех запросов проекта.
JOURNAL_CATCH_LAYER = '/py_modules/'


class JournalCatch(logging.Handler):
    """Обработчик журнала: строка уровня ERROR — строка в таблице проекта.

    Заводится не напрямую, а через `journal_catch_install`: тот следит, чтобы
    обработчик на процесс был один.
    """

    def __init__(self, source: str, write, skip: tuple = (),
                 level: int = JOURNAL_CATCH_LEVEL):
        super().__init__(level=level)
        self.source = source
        self.write = write
        self.skip = tuple(skip)

    def emit(self, record: logging.LogRecord) -> None:
        """Пишет запись в журнал. Не бросает — как и положено обработчику."""
        # ⚠ `getattr`, а не `.flag`: `threading.local` заводит значение своё на
        # поток, и в потоке, который его не ставил, атрибута попросту нет.
        if getattr(_busy, 'flag', False):
            return

        if any(record.name.startswith(one) for one in self.skip):
            return

        _busy.flag = True
        try:
            self.write(source=self.source,
                       message=record.getMessage(),
                       level=record.levelname,
                       logger_name=record.name,
                       traceback_text=journal_catch_traceback(record),
                       context=journal_catch_context(record))
        except Exception:
            # Обработчик журнала не имеет права уронить того, кто журналит.
            self.handleError(record)
        finally:
            _busy.flag = False


def journal_catch_install(source: str, write, *, skip: tuple = (),
                          loggers: tuple = JOURNAL_CATCH_LOGGERS,
                          level: int = JOURNAL_CATCH_LEVEL) -> JournalCatch | None:
    """Подвесить журнал к логгерам процесса. Зовётся рядом с `logging_init()`.

    Args:
        source: чей это процесс — «сервер», «воркер», «кронджоб». Уезжает в
            строку журнала как есть: журнал не место для проверки словаря,
            потерянная запись хуже незнакомой подписи.
        write: чем писать. Зовётся как
            `write(source=, message=, level=, logger_name=, traceback_text=,
            context=)` — то есть проект сам решает, в какую таблицу и с каким
            отпечатком.
        skip: чьи записи пропускать мимо журнала — по началу имени логгера.
            ⚠ Сюда обязан попасть сам модуль журнала проекта: о своих неудачах
            он говорит на уровне INFO, но пожалуйся он когда-нибудь на ERROR —
            запись о недоступной базе пошла бы в ту же недоступную базу.
        loggers: оторванные от корня — см. ⚠⚠ в заголовке.
        level: с какого уровня писать.

    Returns:
        Поставленный обработчик; `None` — если он уже стоял.

    ⚠ **Идемпотентна.** Второй вызов не ставит второй обработчик: у uvicorn
    lifespan запускается заново при перезагрузке кода, и каждая ошибка считалась
    бы дважды.
    """
    global _current

    if _current is not None:
        return None

    handler = JournalCatch(source, write, skip=skip, level=level)
    logging.getLogger().addHandler(handler)

    for name in loggers:
        other = logging.getLogger(name)
        if not other.propagate:
            other.addHandler(handler)

    _current = handler
    _installed_loggers.clear()
    _installed_loggers.extend(loggers)

    return handler


def journal_catch_remove() -> None:
    """Снять обработчик — нужно тестам и разовым скриптам, не приложению."""
    global _current

    if _current is None:
        return

    logging.getLogger().removeHandler(_current)
    for name in _installed_loggers:
        logging.getLogger(name).removeHandler(_current)

    _current = None
    _installed_loggers.clear()


def journal_catch_traceback(record: logging.LogRecord) -> str:
    """Трассировка записи: из исключения, а если его нет — из стека вызова."""
    if journal_catch_failed(record):
        return ''.join(traceback_module.format_exception(*record.exc_info))

    return record.stack_info or ''


def journal_catch_context(record: logging.LogRecord) -> dict:
    """Где случилось. Поля выбраны так, чтобы по ним можно было открыть файл.

    ⚠⚠ **Место берётся из трассировки, а не из полей записи**, когда исключение
    есть. Поля записи показывают, кто позвал логгер, а зовут его через обёртку
    общего слоя (`logger_exception`) — и в журнале у каждой второй ошибки стояло
    бы одно и то же `logging_.py:49`. Последний кадр трассировки показывает, где
    на самом деле сломалось.

    `trace_id` кладёт в запись `logger_info`/`logger_exception` общего слоя — по
    нему ошибка сходится с остальными строками того же прогона.
    """
    context = {'module': record.module,
               'func': record.funcName,
               'line': record.lineno,
               'file': record.pathname,
               'thread': record.threadName,
               'process': record.process}

    if journal_catch_failed(record):
        frame = _frame(traceback_module.extract_tb(record.exc_info[2]))
        if frame:
            context |= {'file': frame.filename, 'line': frame.lineno,
                        'func': frame.name}

    trace_id = getattr(record, 'trace_id', '')
    if trace_id:
        context['trace_id'] = str(trace_id)

    return context


def journal_catch_failed(record: logging.LogRecord) -> bool:
    """Есть ли в записи настоящее исключение.

    ⚠ Проверять `record.exc_info` мало: `logger_exception` зовут и вне `except`,
    и тогда logging кладёт в запись `(None, None, None)` — форматирование такой
    «трассировки» даёт строку `NoneType: None`, которую мы бы сохранили как
    трассировку ошибки.
    """
    return bool(record.exc_info and record.exc_info[1] is not None)


# ── Приватное ────────────────────────────────────────────────────────────────

# Обработчик процесса: он один, и повторная установка это знает.
_current: JournalCatch | None = None

# К каким оторванным логгерам подвесились — их же и снимаем.
_installed_loggers: list = []

# Флаг «мы сейчас внутри записи» — свой на поток: пишут из разных.
_busy = threading.local()
_busy.flag = False


def _frame(frames: list):
    """Кадр трассировки, который стоит показать в строке журнала.

    ⚠⚠ **Не последний кадр, а последний свой.** Последний — почти всегда чужая
    библиотека: у ошибки базы это `pymysql/err.py:154 raise_mysql_exception`,
    одинаковое у всех запросов и ни о чём не говорящее. Человеку нужна строка
    **его** кода, из которой чужую библиотеку позвали, — она и открывается в
    редакторе. Сама чужая часть никуда не девается: трассировка хранится
    целиком.

    ⚠⚠ Общий слой в этом смысле тоже «чужой», хотя код свой, — см.
    `JOURNAL_CATCH_LAYER`. Поэтому предпочтение трёхступенчатое: код проекта →
    общий слой → чужая библиотека.
    """
    if not frames:
        return None

    own = [one for one in frames
           if not any(mark in one.filename for mark in JOURNAL_CATCH_FOREIGN)]
    project = [one for one in own if JOURNAL_CATCH_LAYER not in one.filename]

    return (project or own or frames)[-1]

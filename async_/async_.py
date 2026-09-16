import asyncio
from logging_.logging_ import logger_info

async_loop: asyncio.AbstractEventLoop | None = None
waiting_event: asyncio.Event | None = None
async_loop_tasks: dict = {}


def async_loop_init(loop: asyncio.AbstractEventLoop, loop_task, tread_name):
    """Запомнить event loop приложения и его задачу — чтобы дотянуться до них из чужого потока.

    Зовётся один раз на старте: без этого остальные функции модуля молчат.

    Args:
        loop: работающий event loop приложения.
        loop_task: задача, которую потом снимает `async_loop_task_cancel`.
        tread_name: имя, под которым задача кладётся в реестр.

    ⚠ Loop, событие ожидания и реестр задач лежат в модульных переменных — один
    набор на процесс. Повторный вызов затирает прежний loop, а старое событие
    ожидания остаётся висеть невзведённым.
    """
    global async_loop, waiting_event, async_loop_tasks
    async_loop = loop
    waiting_event = asyncio.Event()
    async_loop_tasks[tread_name] = loop_task


async def async_waiting_start():
    """Встать на паузу — ждать, пока кто-нибудь не позовёт `async_waiting_clear`.

    ⚠ Без `async_loop_init` возвращается сразу и молча: события ожидания просто
    нет. Событие одно на процесс — второй ждущий снимется тем же единственным
    `clear`, что и первый.
    """
    if waiting_event:
        logger_info('Асинхронноее ожидание: включаем')
        waiting_event.clear()
        await waiting_event.wait()


def async_waiting_clear():
    """Снять паузу, поставленную `async_waiting_start`, — можно из чужого потока.

    Флаг взводится через `call_soon_threadsafe`, поэтому вызов безопасен оттуда,
    где event loop приложения не крутится.

    ⚠ Без `async_loop_init` — пустая операция без всякого признака.
    """
    if async_loop and waiting_event:
        logger_info('Асинхронноее ожидание: сброс флага')
        async_loop.call_soon_threadsafe(waiting_event.set)


def async_waiting_is_active() -> bool:
    """Стоит ли сейчас пауза — `True`, пока ожидание не отпустили.

    Returns:
        bool: `False` и когда паузы нет, и когда модуль не инициализирован —
        эти два случая отсюда неразличимы.
    """
    if waiting_event:
        logger_info('Асинхронноее ожидание: активно')
        return not waiting_event.is_set()
    logger_info('Асинхронноее ожидание: не активно')
    return False


def async_loop_task_cancel(tread_name):
    """Снять задачу, зарегистрированную в `async_loop_init`, — из любого потока.

    Args:
        tread_name: имя, под которым задачу клали.

    ⚠ Неизвестное имя — тихая пустая операция. Отмена доезжает не мгновенно:
    `call_soon_threadsafe` лишь ставит её в очередь loop.
    """
    global async_loop, async_loop_tasks
    loop_task = async_loop_tasks.get(tread_name, None)
    if loop_task:
        async_loop.call_soon_threadsafe(loop_task.cancel)

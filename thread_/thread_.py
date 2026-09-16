import threading
import asyncio

from typing import Callable, Any


def thread_with_callback(init_func: Callable, callback_func: Callable, context: Any = None):
    """Выполнить корутину в отдельном потоке и вернуть результат в UI-поток.

    Управление возвращается сразу, результата функция не ждёт. Поток демонический:
    процесс не станет дожидаться его при завершении.

    Args:
        init_func: корутинная функция без аргументов — исполняется через `asyncio.run`.
        callback_func: получает результат; исключение приходит в него **объектом**,
            а не поднимается.
        context: объект с методом `after` (окно Tk) — им коллбэк возвращается в
            UI-поток. Без него результат теряется совсем.

    ⚠ `asyncio.run` заводит внутри потока собственный event loop, поэтому объекты,
    привязанные к loop приложения — пул базы, клиент Redis, — внутри `init_func`
    непригодны.
    """
    def thread_target():
        result = None
        try:
            result = asyncio.run(init_func())
        except Exception as e:
            print(f"❌ Ошибка в потоке парсера: {e}")
            result = e
        finally:
            if context and hasattr(context, 'after'):
                context.after(100, lambda: callback_func(result))

    threading.Thread(target=thread_target, daemon=True).start()
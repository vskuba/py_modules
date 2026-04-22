import threading
import asyncio

from typing import Callable, Any


def thread_with_callback(init_func: Callable, callback_func: Callable, context: Any = None):
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
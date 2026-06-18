import asyncio
import copy
import queue
import traceback

from ai.framework.ai_framework import AiFrameworkModel, AbstractAiFramework
from async_.async_ import async_waiting_is_active, async_waiting_clear
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get

async_task_running: dict[str, asyncio.Task] = {}


async def ai_thread_framework_run(ai_frameworks: list[AbstractAiFramework]):
    while True:
        try:
            framework_model = None

            try:
                framework_model: AiFrameworkModel = queue_get('ai_framework_model').get_nowait()
            except queue.Empty:
                pass

            if not framework_model:
                await asyncio.sleep(0.1)
                continue

            ai_framework: AbstractAiFramework = next(
                (x for x in ai_frameworks if x.__class__.__name__ == framework_model.framework_class))

            if not ai_framework:
                raise ValueError(f'Не могу определить ai_framework')

            if isinstance(framework_model, AiFrameworkModel) and framework_model.name not in async_task_running:
                logger_info(f"🎭 Запуск агента '{framework_model.name}' в асинхронной задаче")

                async_task = asyncio.create_task(ai_framework.framework_run(framework_model))
                async_task_running[framework_model.name] = async_task

                framework_model_copy = copy.deepcopy(framework_model)

                async_task.add_done_callback(
                    lambda t, m=framework_model_copy: async_task_done_callback(t, m)
                )

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Сбой в запуске ai framework: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )


def async_task_done_callback(task, framework_model):
    name = framework_model.name

    try:
        task.result()
    except asyncio.CancelledError:
        logger_info(f"🛑 Async задача '{name}' была отменена")
    except Exception as e:
        logger_info(f"❌ Async задача '{name}' завершилась с ошибкой: {e}\n{traceback.format_exc()}")

    if name in async_task_running:
        del async_task_running[name]

    # Логика sub_thread
    if framework_model.is_sub_agent and async_waiting_is_active():
        async_waiting_clear()

    logger_info(f"✅ Async задача '{name}' завершена")
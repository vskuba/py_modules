import asyncio
import copy
import queue
import traceback

from ai.framework.ai_framework import AiFrameworkModel
from ai.framework.ai_framework_agent import AbstractAiFrameworkAgent
from ai.framework.ai_framework_model_agent import AbstractAiFrameworkAgentModel
from ai.framework.ai_framework_model_task import AbstractAiFrameworkTaskModel
from ai.framework.ai_framework_task import AbstractAiFrameworkTask
from async_.async_ import async_waiting_is_active, async_waiting_clear
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get
from state.state import state_set

async_task_running: dict[str, asyncio.Task] = {}


async def ai_thread_framework_run(ai_frameworks: list, window=None):
    while True:
        try:
            framework_model = None

            try:
                framework_model = queue_get('task').get_nowait()
            except queue.Empty:
                pass

            try:
                framework_model = queue_get('agent').get_nowait()
            except queue.Empty:
                pass

            if not framework_model:
                await asyncio.sleep(0.1)
                continue

            ai_framework = None
            if isinstance(framework_model, AbstractAiFrameworkTaskModel):
                ai_framework = next((x for x in ai_frameworks if isinstance(x, AbstractAiFrameworkTask)), None)
            if isinstance(framework_model, AbstractAiFrameworkAgentModel):
                ai_framework = next((x for x in ai_frameworks if isinstance(x, AbstractAiFrameworkAgent)), None)

            if window:
                if isinstance(framework_model, AbstractAiFrameworkTaskModel):
                    queue_get('chat').put({'text': f'Запускаю задачу {framework_model.title}', 'who': 'agent'})
                if isinstance(framework_model, AbstractAiFrameworkAgentModel):
                    if framework_model.is_sub_thread:
                        queue_get('chat').put({
                            'text': f'Спрашиваю у агента "{framework_model.title}": ' + framework_model.prompt,
                            'who': 'agent'}
                        )
                    else:
                        queue_get('chat').put({'text': f'Запускаю агента "{framework_model.title}"', 'who': 'agent'})

            if not ai_framework:
                raise ValueError(f'Не могу определить ai_framework')

            if isinstance(framework_model, AiFrameworkModel) and framework_model.name not in async_task_running:
                logger_info(f"🎭 Запуск агента '{framework_model.name}' в асинхронной задаче")

                if not framework_model.is_sub_thread:
                    state_set('framework_model_main_thread', framework_model)

                async_task = asyncio.create_task(ai_framework.framework_run(framework_model))
                async_task_running[framework_model.name] = async_task

                framework_model_copy = copy.deepcopy(framework_model)

                async_task.add_done_callback(
                    lambda t, m=framework_model_copy: task_done_callback(t, m, window)
                )

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Сбой в запуске ai framework: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )

def task_done_callback(task, framework_model, window):
    name = framework_model.name

    try:
        task.result()
    except asyncio.CancelledError:
        logger_info(f"🛑 Задача '{name}' была отменена")
    except Exception as e:
        logger_info(f"❌ Задача '{name}' завершилась с ошибкой: {e}\n{traceback.format_exc()}")

    if name in async_task_running:
        del async_task_running[name]

    # Логика sub_thread
    if framework_model.is_sub_thread and async_waiting_is_active():
        async_waiting_clear()

    # Логика уведомления окна
    if not framework_model.is_sub_thread:
        if window:
            window.thread_monitor(framework_model)

    logger_info(f"✅ Задача '{name}' завершена")
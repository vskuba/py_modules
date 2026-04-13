import asyncio
import copy
import queue
import threading
import time
import traceback

from ai_pydantic.framework.ai_framework import AiFrameworkModel, AbstractAiFramework
from ai_pydantic.framework.ai_framework_agent import AbstractAiFrameworkAgent
from ai_pydantic.framework.ai_framework_agent_model import AbstractAiFrameworkAgentModel
from ai_pydantic.framework.ai_framework_task import AbstractAiFrameworkTask
from ai_pydantic.framework.ai_framework_task_model import AbstractAiFrameworkTaskModel
from async_.async_ import async_loop_init, async_waiting_is_active, async_waiting_clear
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get

thread_started: dict[str, AiFrameworkModel] = {}


def ai_thread_run_async(ai_framework: AbstractAiFramework, framework_model: AiFrameworkModel):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop_task = loop.create_task(ai_framework.framework_run(framework_model))

        if not framework_model.is_sub_thread:
            async_loop_init(loop, loop_task, framework_model.name)

        loop.run_until_complete(loop_task)
    except asyncio.CancelledError:
        logger_info("🛑 Поток успешно отменен")
    finally:
        loop.close()


def ai_thread_framework_run(ai_frameworks: list, window=None):
    while True:
        try:
            framework_model = None

            try:
                framework_model = queue_get()['task'].get_nowait()
            except queue.Empty:
                time.sleep(0.1)

            try:
                framework_model = queue_get()['agent'].get_nowait()
            except queue.Empty:
                time.sleep(0.1)

            if not framework_model:
                continue

            ai_framework = None
            if isinstance(framework_model, AbstractAiFrameworkTaskModel):
                ai_framework = next((x for x in ai_frameworks if isinstance(x, AbstractAiFrameworkTask)), None)
            if isinstance(framework_model, AbstractAiFrameworkAgentModel):
                ai_framework = next((x for x in ai_frameworks if isinstance(x, AbstractAiFrameworkAgent)), None)

            if window:
                if isinstance(framework_model, AbstractAiFrameworkTaskModel):
                    queue_get()['chat'].put({'text': f'Запускаю задачу {framework_model.title}', 'who': 'agent'})
                if isinstance(framework_model, AbstractAiFrameworkAgentModel):
                    if framework_model.is_sub_thread:
                        queue_get()['chat'].put({
                            'text': f'Спрашиваю у агента "{framework_model.title}": ' + framework_model.prompt,
                            'who': 'agent'}
                        )
                    else:
                        queue_get()['chat'].put({'text': f'Запускаю агента "{framework_model.title}"', 'who': 'agent'})

            if not ai_framework:
                raise ValueError(f'Не могу определить ai_framework')

            if framework_model.name not in thread_started:
                thread = threading.Thread(
                    target=ai_thread_run_async,
                    args=(ai_framework, framework_model),
                    daemon=True
                )
                thread.start()

                logger_info(f'Старт нового потока "{framework_model.name}"')
                thread_started[framework_model.name] = framework_model

                framework_model_monitor = copy.deepcopy(framework_model)
                thread_monitor(window, thread, framework_model_monitor)

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Сбой в запуске ai framework: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )


def thread_monitor(window, thread: threading.Thread, framework_model: AiFrameworkModel):
    if thread.is_alive():
        threading.Timer(0.5, thread_monitor, args=(window, thread, framework_model)).start()
    else:
        if framework_model.name in thread_started:
            del thread_started[framework_model.name]

        if framework_model.is_sub_thread and async_waiting_is_active():
            async_waiting_clear()

        if not framework_model.is_sub_thread:
            if window:
                window.thread_monitor(framework_model)

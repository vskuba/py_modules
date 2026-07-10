import asyncio
import copy
import inspect
import queue
import time
import traceback

from ai.framework.ai_framework import AiFrameworkModel, AbstractAiFramework
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get

async_task_running: dict[str, asyncio.Task] = {}

# Сколько секунд модель может ждать освобождения занятого агента.
# Согласовано с таймаутом ожидания ответа в чате (60 с): дольше ждать нет смысла —
# HTTP-запрос уже отвалился по 504, а запоздалый ответ собеседнику выдаст бота
AI_FRAMEWORK_QUEUE_TTL = 60.0


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

            # Агент уже выполняется: возвращаем модель в очередь и ждем освобождения.
            # Ошибку пользователю не показываем никогда (собеседник не должен знать,
            # что общается с ботом): либо модель дождется слота, либо будет молча
            # отброшена по TTL с безопасным завершением ожидающего future
            if isinstance(framework_model, AiFrameworkModel) and framework_model.name in async_task_running:
                queued_at: float| None = getattr(framework_model, 'queued_at', None)

                if queued_at is None:
                    framework_model.queued_at = time.monotonic()
                    logger_info(f"⏳ Агент '{framework_model.name}' занят — запрос ждет в очереди")
                    queue_get('ai_framework_model').put(framework_model)
                    await asyncio.sleep(0.1)
                    continue

                if time.monotonic() - queued_at < AI_FRAMEWORK_QUEUE_TTL:
                    queue_get('ai_framework_model').put(framework_model)
                    await asyncio.sleep(0.1)
                    continue

                # TTL истек: ожидающего уже нет (чат отвалился по своему таймауту),
                # а запоздалый ответ вреден. Дропаем молча, но обязательно завершаем
                # future через on_complete — иначе workflow-задача зависнет навсегда
                logger_info(
                    f"🗑 Агент '{framework_model.name}' занят дольше {AI_FRAMEWORK_QUEUE_TTL:.0f} с — "
                    f"запрос отброшен без ответа"
                )
                if framework_model.on_complete:
                    drop_error = TimeoutError(
                        f"Агент '{framework_model.name}' занят: запрос отброшен по TTL очереди."
                    )
                    sig = inspect.signature(framework_model.on_complete)
                    if 'exception' in sig.parameters:
                        await framework_model.on_complete('', exception=drop_error)
                    else:
                        # Колбэк без параметра exception (например, sub-агент, ждущий
                        # queue 'agent_response'): разблокируем его пустым ответом
                        await framework_model.on_complete('')
                continue

            if isinstance(framework_model, AiFrameworkModel):
                logger_info(f"🎭 Запуск агента '{framework_model.name}' в асинхронной задаче")

                async_task = asyncio.create_task(ai_framework.framework_run(framework_model))
                async_task_running[framework_model.name] = async_task

                framework_model_copy = copy.deepcopy(framework_model)

                async_task.add_done_callback(
                    lambda t, m=framework_model_copy: _async_task_done_callback(t, m)
                )

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Сбой в запуске ai framework: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )


def _async_task_done_callback(task, framework_model):
    name = framework_model.name

    try:
        task.result()
    except asyncio.CancelledError:
        logger_info(f"🛑 Async задача '{name}' была отменена")
    except Exception as e:
        logger_info(f"❌ Async задача '{name}' завершилась с ошибкой: {e}\n{traceback.format_exc()}")

    if name in async_task_running:
        del async_task_running[name]

    logger_info(f"✅ Async задача '{name}' завершена")

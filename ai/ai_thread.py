import asyncio
import copy
import inspect
import queue
import time
import traceback

from ai.framework.ai_framework import AiFrameworkModel, AbstractAiFramework
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get

# (agent_name) → list of (asyncio.Task, llm_id) — активные слоты на агент
async_task_running: dict[str, list[tuple[asyncio.Task, int]]] = {}

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

            if isinstance(framework_model, AiFrameworkModel):
                name = framework_model.name
                parallel_max = getattr(framework_model, 'llm_parallel_per_agent_max', 1)

                # Убираем завершённые слоты (callback мог не успеть очиститься)
                running = [(t, lid) for t, lid in async_task_running.get(name, []) if not t.done()]
                if running:
                    async_task_running[name] = running
                elif name in async_task_running:
                    del async_task_running[name]
                    running = []

                if len(running) >= parallel_max:
                    # Все слоты заняты: возвращаем в очередь и ждём
                    # Ошибку пользователю не показываем никогда (собеседник не должен знать,
                    # что общается с ботом): либо модель дождется слота, либо будет молча
                    # отброшена по TTL с безопасным завершением ожидающего future
                    queued_at: float | None = getattr(framework_model, 'queued_at', None)

                    if queued_at is None:
                        framework_model.queued_at = time.monotonic()
                        logger_info(f"⏳ Агент '{name}' занят ({len(running)}/{parallel_max} слотов) — запрос ждет в очереди")
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
                        f"🗑 Агент '{name}' занят дольше {AI_FRAMEWORK_QUEUE_TTL:.0f} с — "
                        f"запрос отброшен без ответа"
                    )
                    if framework_model.on_complete:
                        drop_error = TimeoutError(
                            f"Агент '{name}' занят: запрос отброшен по TTL очереди."
                        )
                        sig = inspect.signature(framework_model.on_complete)
                        if 'exception' in sig.parameters:
                            await framework_model.on_complete('', exception=drop_error)
                        else:
                            await framework_model.on_complete('')
                    continue

                # Слот свободен — выбираем LLM, которая не занята в активных слотах
                active_llm_ids = {lid for _, lid in running}
                entities_llm = getattr(framework_model, 'entities_llm', [])
                next_llm = next(
                    (llm for llm in entities_llm if llm.get('id') not in active_llm_ids),
                    framework_model.entity_llm_current
                )

                logger_info(
                    f"🎭 Запуск агента '{name}' "
                    f"(слот {len(running) + 1}/{parallel_max}, LLM {next_llm.get('name', '?')})"
                )

                # Копия модели — каждый параллельный слот работает независимо
                # (entity_llm_current и прочий mutable-state не должны делиться между слотами)
                model_copy = copy.deepcopy(framework_model)
                model_copy.entity_llm_current = next_llm
                model_copy.queued_at = None

                async_task = asyncio.create_task(ai_framework.framework_run(model_copy))

                if name not in async_task_running:
                    async_task_running[name] = []
                async_task_running[name].append((async_task, next_llm.get('id') or 0))

                async_task.add_done_callback(
                    lambda t, n=name: _async_task_done_callback(t, n)
                )

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Сбой в запуске ai framework: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )


def _async_task_done_callback(task, name):
    try:
        task.result()
    except asyncio.CancelledError:
        logger_info(f"🛑 Async задача '{name}' была отменена")
    except Exception as e:
        logger_info(f"❌ Async задача '{name}' завершилась с ошибкой: {e}\n{traceback.format_exc()}")

    if name in async_task_running:
        async_task_running[name] = [(t, lid) for t, lid in async_task_running[name] if t is not task]
        if not async_task_running[name]:
            del async_task_running[name]

    logger_info(f"✅ Async задача '{name}' завершена")

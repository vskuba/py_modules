import asyncio
import queue
import traceback

from abc import abstractmethod

from ai.framework.ai_framework import AbstractAiFramework
from ai.framework.ai_framework_manager_task import AbstractAiFrameworkTaskManager
from ai.framework.ai_framework_model_task import AbstractAiFrameworkTaskModel
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get


class AbstractAiFrameworkTask(AbstractAiFramework):
    def __init__(self, framework_manager: AbstractAiFrameworkTaskManager):
        super().__init__(framework_manager)
        self.framework_manager: AbstractAiFrameworkTaskManager = framework_manager

    @abstractmethod
    def task_history_filename_get(self, task: AbstractAiFrameworkTaskModel) -> str:
        pass

    @abstractmethod
    def task_history_dir_get(self) -> str:
        pass

    @abstractmethod
    def task_history_save(self, result, task: AbstractAiFrameworkTaskModel):
        pass

    @abstractmethod
    def task_history_load(self, task: AbstractAiFrameworkTaskModel) -> list:
        pass

    async def framework_run(self, framework_model: AbstractAiFrameworkTaskModel):
        catch_exception: bool = False
        while True:
            await asyncio.sleep(0.1)

            try:
                queue_get('task_abort').get_nowait()
                logger_info(f'🛑 Команда на остановку задачи "{framework_model.name}"')
                await self.framework_report(framework_model)
                return
            except queue.Empty:
                pass

            try:
                if catch_exception and framework_model:
                    logger_info('Возобновляем задачу..')
                    catch_exception = False

                logger_info('🚀 Задача из очереди: "%s"' % framework_model.name)

                self.framework_manager.task_active_set(framework_model)

                await self.engine_prepare(framework_model)

                result = await self.engine_run(framework_model)
                response = await self.engine_result_handle(result, framework_model)

                if response is not None:
                    if framework_model.on_complete:
                        await framework_model.on_complete(response.text)

                if self.framework_manager.is_complete(framework_model):
                    self.framework_manager.task_active_set(None)
                    queue_get('task_complete').put(True)
                    await self.framework_report(framework_model)
                    return
                else:
                    framework_model = self.framework_manager.switch_next_sub_task(framework_model)
                    queue_get('task_continue').put(framework_model)
                    await self.framework_report(framework_model)
                    continue

            except Exception as e:
                backtrace = traceback.format_exc()
                logger_info(
                    f"❌ Сбой в цикле агента: {e}."
                    f"Полный стек вызовов:\n{backtrace}"
                )

                queue_get('chat').put({"text": 'Произошла ошибка: %s' % e, "who": 'agent'})

                # rate limit handling
                if getattr(e, 'status_code', None) in [429, 413]:
                    await self.framework_report(framework_model)
                    return

                queue_get('chat').put({"text": 'Работа будет продолжена', "who": 'agent'})
                catch_exception = True
                continue
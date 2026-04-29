import asyncio
import queue
import traceback

from ai.framework.ai_framework import AbstractAiFramework
from ai.framework.ai_framework_manager_agent import AbstractAiFrameworkAgentManager
from ai.framework.ai_framework_model_agent import AbstractAiFrameworkAgentModel
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get


class AbstractAiFrameworkAgent(AbstractAiFramework):
    def __init__(self, framework_manager: AbstractAiFrameworkAgentManager):
        super().__init__(framework_manager)
        self.framework_manager: AbstractAiFrameworkAgentManager = framework_manager

    async def framework_run(self, framework_model: AbstractAiFrameworkAgentModel):
        catch_exception: bool = False
        catch_exception_retry_attempt: int = 0
        while True:
            await asyncio.sleep(0.1)

            try:
                queue_get('agent_abort').get_nowait()
                logger_info(f'🛑 Команда на остановку агента "{framework_model.name}"')
                await self.message_history_save(framework_model)
                return
            except queue.Empty:
                pass

            try:
                if catch_exception_retry_attempt > 3:
                    catch_exception_retry_attempt = 0
                    return

                if catch_exception and framework_model:
                    logger_info('Возобновляем агента..')
                    catch_exception_retry_attempt += 1

                await self.engine_prepare(framework_model)

                result = await self.engine_run(framework_model)
                response = await self.engine_result_handle(result, framework_model)

                if response is not None:
                    if framework_model.on_complete:
                        await framework_model.on_complete(response.text)

                await self.message_history_save(framework_model)
                return

            except Exception as e:
                backtrace = traceback.format_exc()
                logger_info(
                    f"❌ Сбой в цикле агента: {e}."
                    f"Полный стек вызовов:\n{backtrace}"
                )

                if framework_model.is_gui_mode:
                    queue_get('chat').put({"text": 'Произошла ошибка: %s' % e, "who": 'agent'})

                # rate limit handling
                error_code = getattr(e, 'status_code', None)
                if error_code in [429, 413]:
                    logger_info(
                        f"❌ Код ошибки: {error_code}."
                    )
                    await self.message_history_save(framework_model)
                    return

                if framework_model.is_gui_mode:
                    queue_get('chat').put({"text": 'Работа будет продолжена', "who": 'agent'})

                catch_exception = True
                continue

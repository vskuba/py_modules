import asyncio
import queue
import traceback

from pydantic_ai import Agent, AgentRunResult, ModelSettings

from ai.ai_mcp import AiMcpManager
from ai.framework.ai_framework import AbstractAiFramework, AiFrameworkResult
from ai.framework.agent.ai_framework_manager import AiFrameworkAgentManager
from ai.framework.agent.ai_framework_model import AiFrameworkAgentModel
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get


class AiFrameworkAgent(AbstractAiFramework):
    def __init__(self, framework_manager: AiFrameworkAgentManager):
        super().__init__(framework_manager)
        self.framework_manager: AiFrameworkAgentManager = framework_manager

    async def engine_prepare(self, framework_model: AiFrameworkAgentModel) -> Agent:
        """
        Создает типизированного агента Pydantic-AI.
        Результат работы агента всегда будет объектом ResponseModel.
        """
        model = self.llm_model_get(framework_model)

        message_history = await self.message_history_get(framework_model)

        logger_info(
            f'🧠 Передаем историю агенту:' 
            f'{message_history}' if message_history else "\nВ истории пусто или отключена"
        )

        engine = Agent(
            model,
            system_prompt=framework_model.system_prompt.strip() + message_history,
            output_type=framework_model.response_model,
            retries=10,
            output_retries=10
        )

        self.engine_storage[framework_model.name] = engine
        self.engine_tools_local_add(engine, framework_model)

        return engine

    async def engine_run(self, framework_model: AiFrameworkAgentModel):
        engine = self.engine_storage[framework_model.name]
        ai_mcp_manager = AiMcpManager()

        try:
            if framework_model.mcp_servers:
                await ai_mcp_manager.mcp_server_add_tools(engine, framework_model)

            logger_info(f'🚀 Запускаем модель агента... {framework_model}')

            return await engine.run(
                user_prompt=framework_model.prompt,
                model_settings=ModelSettings(
                    parallel_tool_calls=True,
                    temperature=0.0,
                )
            )
        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка агента {framework_model.name}: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise e

        finally:
            if framework_model.mcp_servers:
                logger_info(f'Закрываем MCP сессии для задачи {framework_model.name}...')
                await ai_mcp_manager.mcp_server_close_all()

    async def engine_result_handle(
            self, result: AgentRunResult,
            framework_model: AiFrameworkAgentModel
    ) -> AiFrameworkResult | None:
        await super().engine_result_handle(result, framework_model)

        # если ответ просто текст, а не модель
        if not result.output or isinstance(result.output, str):
            return AiFrameworkResult(str(result.output))

        return AiFrameworkResult(result.output.text)

    async def framework_run(self, framework_model: AiFrameworkAgentModel):
        _exception: Exception | None = None
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
                if _exception and framework_model:
                    if catch_exception_retry_attempt > 1:
                        logger_info('Возобновление агента повторно привело к ошибке..')
                        await self._error_complete(framework_model, _exception)
                        return

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

                error_text = 'Произошла ошибка: %s' % e
                if framework_model.is_gui_mode:
                    queue_get('chat').put({"text": error_text, "who": 'agent'})

                # rate limit handling
                error_code = getattr(e, 'status_code', None)
                if error_code in [429, 413]:
                    logger_info(
                        f"❌ Код ошибки: {error_code}."
                    )
                    await self.message_history_save(framework_model)
                    await self._error_complete(framework_model, e)

                    return

                if framework_model.is_gui_mode:
                    queue_get('chat').put({"text": 'Работа будет продолжена', "who": 'agent'})

                _exception = e
                continue

    async def _error_complete(self, framework_model: AiFrameworkAgentModel, e: Exception):
        error_text = 'Произошла ошибка: %s' % e
        if framework_model.on_complete:
            await framework_model.on_complete(error_text)
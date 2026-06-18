import asyncio
import os
import queue
import traceback
from pathlib import Path

from pydantic import TypeAdapter
from pydantic_ai import Agent, AgentRunResult, ModelMessage, ModelSettings

from ai.ai_mcp import AiMcpManager
from ai.framework.ai_framework import AbstractAiFramework, AiFrameworkResult
from ai.framework.task.ai_framework_manager import AiFrameworkTaskManager
from ai.framework.task.ai_framework_model import AiFrameworkTaskModel
from config.config import config_get
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get


class AiFrameworkTask(AbstractAiFramework):
    def __init__(self, framework_manager: AiFrameworkTaskManager):
        super().__init__(framework_manager)
        self.framework_manager: AiFrameworkTaskManager = framework_manager

    async def engine_prepare(self, framework_model: AiFrameworkTaskModel) -> Agent:
        """
        Создает типизированного агента Pydantic-AI.
        Результат работы агента всегда будет объектом ResponseModel.
        """
        index = framework_model.name + str(framework_model.sub_task_current_index)
        if index in self.engine_storage:
            return self.engine_storage[index]

        model = self.llm_model_get(framework_model)

        engine = Agent(
            model,
            system_prompt=framework_model.prompt_system.strip(),
            output_type=framework_model.response_model,
            retries=10,
            output_retries=10
        )

        self.engine_storage[framework_model.name] = engine

        # if not self.message_history:
        #     self.message_history[framework_model.name] = self.task_history_load(framework_model)

        self.engine_tools_local_add(engine, framework_model)

        return engine

    async def engine_run(self, framework_model: AiFrameworkTaskModel):
        # message_history = self.message_history_get(framework_model)

        logger_info(
            f'🧱 Подзадача {framework_model.sub_task_current_index + 1}/{len(framework_model.yaml.sub_tasks)}, '
            f'prompt: {framework_model.prompt_user}'
        )
        # logger_info(
        #     f'🧠 Передаем историю агенту:\n'
        #     f'{message_history if message_history else "пока ничего нет"}'
        # )

        engine = self.engine_storage[framework_model.name]
        ai_mcp_manager = AiMcpManager()

        try:
            if framework_model.mcp_servers:
                await ai_mcp_manager.mcp_server_add_tools(engine, framework_model)

            return await engine.run(
                user_prompt=framework_model.prompt_user,
                # message_history=self.history,
                model_settings=ModelSettings(
                    parallel_tool_calls=True,
                    temperature=0.0,
                )
            )
        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка при выполнении задачи {framework_model.name}: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise e

        finally:
            if framework_model.mcp_servers:
                logger_info(f'Закрываем MCP сессии для задачи {framework_model.name}...')
                await ai_mcp_manager.mcp_server_close_all()

    async def engine_result_handle(
            self,
            result: AgentRunResult,
            framework_model: AiFrameworkTaskModel
    ) -> AiFrameworkResult | None:
        await super().engine_result_handle(result, framework_model)

        response_model_dict = result.output.model_dump()
        del response_model_dict['text']

        framework_model.input_values = framework_model.input_values | response_model_dict

        logger_info(f'🧠 Получили новые сообщения истории в задаче "{framework_model.title}"'
                    f' ({framework_model.sub_task_current_index + 1}/{len(framework_model.yaml.sub_tasks)})\n'
                    f' {self.message_history}')

        if framework_model.history_save:
            self.task_history_save(result, framework_model)

        return AiFrameworkResult(result.output.text)

    def task_history_save(self, result, task: AiFrameworkTaskModel):
        messages = result.all_messages()

        filename = self.task_history_filename_get(task)
        file_path = Path(filename)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(filename, 'wb') as f:
            f.write(TypeAdapter(list[ModelMessage]).dump_json(messages))

        logger_info('🧠 Сохранили историю в файл')

    def task_history_load(self, task: AiFrameworkTaskModel) -> list[ModelMessage]:
        filename = self.task_history_filename_get(task)
        if not os.path.exists(filename):
            return []

        with open(filename, 'rb') as f:
            data = f.read()

            return TypeAdapter(list[ModelMessage]).validate_json(data)

    def task_history_dir_get(self) -> str:
        return config_get('data_dir') + '/' + config_get('history_dir')

    def task_history_filename_get(self, task: AiFrameworkTaskModel) -> str:
        return '%s/%s_history.json' % (self.task_history_dir_get(), task.name)

    async def framework_run(self, framework_model: AiFrameworkTaskModel):
        catch_exception: bool = False
        while True:
            await asyncio.sleep(0.1)

            try:
                queue_get('task_abort').get_nowait()
                logger_info(f'🛑 Команда на остановку задачи "{framework_model.name}"')
                await self.message_history_save(framework_model)
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
                    await self.message_history_save(framework_model)
                    return
                else:
                    framework_model = self.framework_manager.switch_next_sub_task(framework_model)
                    queue_get('task_continue').put(framework_model)
                    await self.message_history_save(framework_model)
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
                    await self.message_history_save(framework_model)
                    return

                queue_get('chat').put({"text": 'Работа будет продолжена', "who": 'agent'})
                catch_exception = True
                continue

import os
import traceback

from pydantic import TypeAdapter
from pathlib import Path
from pydantic_ai import Agent, AgentRunResult, ModelMessage, ModelSettings

from ai.ai_mcp import AiMcpManager
from ai.framework.ai_framework import AiFrameworkResult
from ai.framework.task.ai_framework import AbstractAiFrameworkTask
from ai.framework.task.ai_framework_manager_pydantic import AiFrameworkTaskManagerPydantic
from ai.framework.task.ai_framework_model_pydantic import AiFrameworkTaskModelPydantic
from config.config import config_get
from logging_.logging_ import logger_info


class AiFrameworkTaskPydantic(AbstractAiFrameworkTask):
    def __init__(self, framework_manager: AiFrameworkTaskManagerPydantic):
        super().__init__(framework_manager)
        self.framework_manager: AiFrameworkTaskManagerPydantic = framework_manager

    async def engine_prepare(self, framework_model: AiFrameworkTaskModelPydantic) -> Agent:
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
            system_prompt=framework_model.system_prompt.strip(),
            output_type=framework_model.response_model,
            retries=10,
            output_retries=10
        )

        self.engine_storage[framework_model.name] = engine

        # if not self.message_history:
        #     self.message_history[framework_model.name] = self.task_history_load(framework_model)

        self.engine_tools_local_add(engine, framework_model)

        return engine

    async def engine_run(self, framework_model: AiFrameworkTaskModelPydantic):
        # message_history = self.message_history_get(framework_model)

        logger_info(
            f'🧱 Подзадача {framework_model.sub_task_current_index + 1}/{len(framework_model.yaml.sub_tasks)}, '
            f'prompt: {framework_model.prompt}'
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
                user_prompt=framework_model.prompt,
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
            framework_model: AiFrameworkTaskModelPydantic
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

    def task_history_save(self, result, task: AiFrameworkTaskModelPydantic):
        messages = result.all_messages()

        filename = self.task_history_filename_get(task)
        file_path = Path(filename)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(filename, 'wb') as f:
            f.write(TypeAdapter(list[ModelMessage]).dump_json(messages))

        logger_info('🧠 Сохранили историю в файл')

    def task_history_load(self, task: AiFrameworkTaskModelPydantic) -> list[ModelMessage]:
        filename = self.task_history_filename_get(task)
        if not os.path.exists(filename):
            return []

        with open(filename, 'rb') as f:
            data = f.read()

            return TypeAdapter(list[ModelMessage]).validate_json(data)

    def task_history_dir_get(self) -> str:
        return config_get('data_dir') + '/' + config_get('history_dir')

    def task_history_filename_get(self, task: AiFrameworkTaskModelPydantic) -> str:
        return '%s/%s_history.json' % (self.task_history_dir_get(), task.name)

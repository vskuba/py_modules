import json
import os
import re
import traceback
from functools import partial

import yaml

from pathlib import Path
from typing import Any, Tuple, List

from pydantic import BaseModel, Field, create_model

from ai.framework.ai_framework import AbstractAiFrameworkManager
from ai.framework.task.ai_framework_model import AiTaskYaml, AiFrameworkTaskModel
from ai.tool.ai_tool import ai_tools_get, ai_tools_permanent_get
from config.config import config_get
from logging_.logging_ import logger_info
from mcp_.mcp_ import mcp_config_get
from queue_.queue_ import queue_get


class AiFrameworkTaskManager(AbstractAiFrameworkManager):
    def __init__(self):
        self.active_task: AiFrameworkTaskModel | None = None

    def create_framework_model(self, agent_entity: dict) -> AiFrameworkTaskModel:
        pass

    def dir(self) -> str:
        return config_get('data_dir') + '/' + config_get('task_dir')

    def list(self) -> list[str]:
        if not os.path.exists(self.dir()):
            return []

        return [os.path.splitext(f)[0] for f in os.listdir(self.dir()) if f.endswith('.yml')]

    def filepath_get(self, name: str) -> str:
        if name not in self.list():
            raise ValueError("Task with name '%s' not found" % name)

        return self.dir() + '/' + name + '.yml'

    def load(self, name: str) -> AiFrameworkTaskModel:
        try:
            with open(self.filepath_get(name), 'r') as f:
                yaml_dict = yaml.safe_load(f)

            yaml_dict['name'] = name
            task_yaml = AiTaskYaml(**yaml_dict)

            self.validate(task_yaml)

            task = self.create(task_yaml, {}, 0)
            task.yaml = task_yaml

            return task

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Файл {name}.yml загрузка ошибка: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

    def create(
            self,
            task_yaml: AiTaskYaml,
            input_values: dict[str, Any],
            sub_task_index: int = 0
    ) -> AiFrameworkTaskModel:
        try:
            title = task_yaml.title
            sub_task_current = task_yaml.sub_tasks[sub_task_index]

            prompt_system = task_yaml.system_prompt
            pattern = r"\{env:([^}]+)}"
            matches = re.findall(pattern, prompt_system)
            if matches:
                for var_name in matches:
                    env_value = os.getenv(var_name, "")
                    prompt_system = prompt_system.replace(f"{{env:{var_name}}}", env_value)

            prompt = sub_task_current.prompt

            prompt, response_model, response_model_input_values = self.prompt_parse_response_model(prompt)

            input_values = input_values | response_model_input_values

            prompt, input_fields = self.prompt_parse_input_fields(prompt, input_values, response_model)

            framework_model = AiFrameworkTaskModel(
                title=title,
                yaml=task_yaml,
                sub_task_current_index=sub_task_index,
                input_fields=input_fields
            )

            framework_model.name = task_yaml.name
            framework_model.prompt = prompt
            framework_model.system_prompt = prompt_system
            framework_model.tools = sub_task_current.tools if sub_task_current.tools else []
            framework_model.mcp_servers = task_yaml.mcp_servers
            framework_model.input_values_set(input_values)
            framework_model.input_values_set(input_values)
            framework_model.history_save = task_yaml.history_save
            framework_model.response_model = response_model

            async def on_complete(response_text: str):
                queue_get('chat').put({"text": response_text, "who": 'agent'})

            framework_model.on_complete = partial(on_complete)

            return framework_model

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка при создании задачи {task_yaml.name}: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

    def prompt_parse_response_model(self, prompt: str) -> Tuple[str, BaseModel, dict[str, str]]:
        type_mapping = {
            'str': str,
            'bool': bool,
            'int': int,
            'float': float,
            'list': list
        }
        # Формируем поля для нашей модели
        pattern = r"\{\{([^}|]+)\|([^:]+):(" + "|".join(type_mapping) + r")\}\}"
        matches = re.findall(pattern, prompt)

        # ожидаем final_result только если нужна model response
        response_model_fields = {
            'text': (str, Field(description="Final result"))
        }
        response_model_input_values = {}

        if matches:
            response_model_fields \
                = {
                      var: (type_mapping.get(t, Any), Field(description=desc)) for desc, var, t in
                      matches
                  } | response_model_fields
            response_model_input_values = {var: name for name, var, _ in matches}
            prompt = re.sub(r"\{\{([^}|]+)\|[^:]+:(" + "|".join(type_mapping) + r")}}", r"\1", prompt)

        return prompt, create_model('ResponseModel', **response_model_fields)(), response_model_input_values

    def prompt_parse_input_fields(
            self, prompt: str,
            input_values: dict[str, Any],
            response_model: BaseModel
    ) -> Tuple[str, List[str]]:

        pattern = r"\{\{(.*?)\}\}"
        input_fields = re.findall(pattern, prompt)

        # 2. БЕЗОПАСНАЯ ЗАМЕНА (Вместо prompt.format)
        # Заменяем только те ключи, которые обернуты в двойные скобки
        for key, value in input_values.items():
            placeholder = f"{{{{{key}}}}}"  # Это строка "{{key}}"
            if placeholder in prompt:
                prompt = prompt.replace(placeholder, str(value))

        # 3. Очистка: убираем лишние пробелы и оставшиеся неиспользованные {{...}}
        # Важно: используем нежадный поиск .*?, чтобы не удалить лишнего между тегами
        prompt = re.sub(r"\{\{.*?}}", '', prompt)
        prompt = re.sub(r'\s+', ' ', prompt).strip()

        # --- Далее ваш существующий код для блоков <input_data> и <expect_data> ---
        input_and_expected_data = []
        if input_values:
            json_input_str = json.dumps(input_values, ensure_ascii=False)
            input_and_expected_data.append(f"<input_data>{json_input_str}</input_data>")

        json_response_model = response_model.model_json_schema()
        # Безопасное получение типов для схемы
        json_response_model_str = {
            k: p.get('type', 'any')
            for k, p in json_response_model.get('properties', {}).items()
        }
        input_and_expected_data.append(f"<expect_data>{json_response_model_str}</expect_data>")

        prompt = '\n'.join(input_and_expected_data) + f'\n\n{prompt}'

        return prompt, input_fields

    def validate(self, task_yaml: AiTaskYaml):
        try:

            mcp_tools = []
            if task_yaml.mcp_servers:
                for mcp_server_name in task_yaml.mcp_servers:
                    mcp_server_name = mcp_server_name.lower()
                    mcp_config = mcp_config_get(mcp_server_name)

                    if not mcp_config:
                        raise ValueError(f"MCP сервер '{mcp_server_name}' не найден")

                    mcp_tools += [t.name for t in mcp_config.tools]
            local_tools = [t.__name__ for t in ai_tools_get() + ai_tools_permanent_get()]
            tools = local_tools + mcp_tools

            input_values = {}
            for sub_task in task_yaml.sub_tasks:

                for tool_name in sub_task.tools or []:
                    if not any(t == tool_name for t in tools):
                        raise ValueError(f"Инструмент '{tool_name}' не найден в доступных ai_tools и mcp_tools.")

                # validate input_fields
                prompt = sub_task.prompt
                prompt, response_model, response_model_input_values = self.prompt_parse_response_model(prompt)
                input_values = input_values | response_model_input_values
                self.prompt_parse_input_fields(prompt, input_values, response_model)

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Файл {task_yaml.name}.yml validate ошибка: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise Exception(f'Variable {e} is not defined in input fields.')

    def switch_next_sub_task(self, task: AiFrameworkTaskModel) -> AiFrameworkTaskModel:
        sub_task = self.create(task.yaml, task.input_values, task.sub_task_current_index + 1)
        sub_task.id = task.id
        sub_task.yaml = task.yaml

        return sub_task

    def is_complete(self, task: AiFrameworkTaskModel) -> bool:
        return len(task.yaml.sub_tasks) <= (task.sub_task_current_index + 1)

    def task_active_set(self, task: AiFrameworkTaskModel | None):
        self.active_task = task

    def task_active_get(self):
        return self.active_task

    def task_schema_yaml_update(self, name: str, key: str, value: str | bool):
        filepath = self.filepath_get(name)
        path = Path(filepath)

        if not path.exists():
            logger_info(f"Ошибка: Файл {filepath} не найден.")
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.load(f, Loader=yaml.FullLoader) or {}

            data[key] = value

            with open(path, 'w', encoding='utf-8') as f:
                # allow_unicode=True сохранит кириллицу (например, в заголовках)
                # sort_keys=False сохранит текущий порядок ключей
                yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

            logger_info(f"Обновлено: {key} = {value} в файле {name}")

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка при обновлении YAML {name}.yml: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
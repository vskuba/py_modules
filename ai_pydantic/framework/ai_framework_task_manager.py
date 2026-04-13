import traceback
import yaml

from abc import abstractmethod
from pathlib import Path
from typing import Any

from ai_pydantic.framework.ai_framework import AbstractAiFrameworkManager
from ai_pydantic.framework.ai_framework_task_model import AbstractAiFrameworkTaskModel, AiTaskYaml
from logging_.logging_ import logger_info


class AbstractAiFrameworkTaskManager(AbstractAiFrameworkManager):
    def __init__(self):
        self.active_task: AbstractAiFrameworkTaskModel | None = None

    def load(self, name: str) -> AbstractAiFrameworkTaskModel:
        try:
            with open(self.filepath_get(name), 'r') as f:
                yaml_dict = yaml.safe_load(f)

            task_yaml = AiTaskYaml(**yaml_dict)
            task_yaml.filename = name

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

    @abstractmethod
    def dir(self) -> str:
        pass

    @abstractmethod
    def list(self) -> list[str]:
        pass

    @abstractmethod
    def filepath_get(self, name: str) -> str:
        pass

    @abstractmethod
    def create(
            self,
            task_yaml: AiTaskYaml,
            input_values: dict[str, Any],
            sub_task_index: int = 0) -> AbstractAiFrameworkTaskModel:
        pass

    @abstractmethod
    def validate(self, task_yaml: AiTaskYaml):
        pass

    def switch_next_sub_task(self, task: AbstractAiFrameworkTaskModel) -> AbstractAiFrameworkTaskModel:
        sub_task = self.create(task.yaml, task.input_values, task.sub_task_current_index + 1)
        sub_task.id = task.id
        sub_task.yaml = task.yaml

        return sub_task

    def is_complete(self, task: AbstractAiFrameworkTaskModel) -> bool:
        return len(task.yaml.sub_tasks) <= (task.sub_task_current_index + 1)

    def task_active_set(self, task: AbstractAiFrameworkTaskModel | None):
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
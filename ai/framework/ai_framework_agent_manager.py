import traceback
import yaml

from abc import abstractmethod

from ai.framework.ai_framework import AbstractAiFrameworkManager
from ai.framework.ai_framework_agent_model import AiAgentYaml, AbstractAiFrameworkAgentModel
from logging_.logging_ import logger_info


class AbstractAiFrameworkAgentManager(AbstractAiFrameworkManager):
    @abstractmethod
    def create(self, agent_yaml: AiAgentYaml) -> AbstractAiFrameworkAgentModel:
        pass

    @abstractmethod
    def filepath_get(self, name: str) -> str:
        pass

    def load_yaml(self, name: str) -> dict:
        path = self.filepath_get(name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                return data

        except FileNotFoundError:
            logger_info(f"📂 Файл агента {name} не найден по пути: {path}")
            raise
        except yaml.YAMLError as e:
            logger_info(f"⚠️ Ошибка синтаксиса в YAML файле {name}: {e}")
            raise
        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Агент {name} ошибка: {e}.\n"
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

    def load(self, name: str) -> AbstractAiFrameworkAgentModel:
        try:
            yaml_dict = self.load_yaml(name)
            yaml_dict['name'] = name
            agent_yaml = AiAgentYaml(**yaml_dict)

            return self.create(agent_yaml)

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Агент {name} ошибка: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

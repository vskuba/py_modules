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

    def load(self, name: str) -> AbstractAiFrameworkAgentModel:
        try:
            with open(self.filepath_get(name), 'r') as f:
                yaml_dict = yaml.safe_load(f)

            yaml_dict.name = name

            agent_yaml = AiAgentYaml(**yaml_dict)
            agent_yaml.filename = name

            return self.create(agent_yaml)

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Агент {name} ошибка: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

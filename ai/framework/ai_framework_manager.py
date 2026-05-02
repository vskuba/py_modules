from abc import ABC, abstractmethod
from dataclasses import dataclass

from ai.framework.ai_framework_model import AiFrameworkModel


@dataclass
class AbstractAiFrameworkManager(ABC):
    @abstractmethod
    def create_framework_model(self, entity: dict) -> AiFrameworkModel:
        pass

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ai.framework.ai_framework_model import AiFrameworkModel


@dataclass
class AbstractAiFrameworkManager(ABC):
    @abstractmethod
    def load(self, name: str) -> AiFrameworkModel:
        pass

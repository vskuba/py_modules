from abc import ABC, abstractmethod
from dataclasses import dataclass

from ai.framework.ai_framework_model import AiFrameworkModel


@dataclass
class AbstractAiFrameworkManager(ABC):
    @abstractmethod
    async def framework_model_create(self, metadata: dict) -> AiFrameworkModel:
        pass

    @abstractmethod
    async def prompt_system_create(self, metadata: dict) -> str:
        pass

    @abstractmethod
    async def prompt_user_create(self, metadata: dict) -> str:
        pass

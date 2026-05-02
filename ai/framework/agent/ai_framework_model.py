import uuid

from ai.framework.ai_framework import AiFrameworkModel


class AiFrameworkAgentModel(AiFrameworkModel):
    def __init__(
            self,
            title: str,
            description: str,
            specialization: str
    ):
        self.id = str(uuid.uuid4())
        self.title = title
        self.description = description
        self.specialization = specialization

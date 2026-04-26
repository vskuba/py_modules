import uuid

from dataclasses import field
from typing import Optional
from pydantic import BaseModel

from ai.framework.ai_framework import AiFrameworkModel


class AiAgentYaml(BaseModel):
    title: str
    description: str
    system_prompt: str
    specialization: str | None = None
    memory_short_disabled: bool = False
    memory_short_length: int = 10
    tools: Optional[list[str]] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)


class AbstractAiFrameworkAgentModel(AiFrameworkModel):
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

import uuid

from dataclasses import field
from typing import Optional
from pydantic import BaseModel

from ai.framework.ai_framework import AiFrameworkModel


class AiAgentYaml(BaseModel):
    title: str
    description: str
    system_prompt: str
    filename: str
    memory_short_disabled: bool = False
    specialization: str | None = None
    tools: Optional[list[str]] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)


class AbstractAiFrameworkAgentModel(AiFrameworkModel):
    def __init__(
            self,
            name: str,
            title: str,
            description: str,
            prompt: str,
            system_prompt: str,
            specialization: str
    ):
        self.id = str(uuid.uuid4())
        self.name = name
        self.title = title
        self.description = description
        self.prompt: str = prompt
        self.system_prompt = system_prompt
        self.specialization = specialization

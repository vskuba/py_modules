from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel


@dataclass
class AiFrameworkModel:
    name: str
    prompt: str
    system_prompt: str
    user_id: int
    tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    on_complete: Callable | None = None
    is_sub_agent: bool = False
    is_gui_mode: bool = True
    session_disabled: bool = False
    memory_short_length: int = 10
    session_uuid: str | None = None
    llm: str | None = None
    response_model: str | BaseModel = str
    entity: dict = None
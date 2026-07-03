from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel


@dataclass
class AiFrameworkModel:

    # params for framework using
    framework_class = str
    name: str
    prompt_user: str
    prompt_system: str
    user_id: int
    request_uuid: str
    session_uuid: str
    companion_id: int = 0
    tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    on_complete: Callable | None = None
    is_gui_mode: bool = True
    is_transition: bool = False
    session_disabled: bool = False
    memory_short_length: int = 10
    entity_agent: dict = field(default_factory=dict)
    entity_llm: dict = field(default_factory=dict)
    response_model: str | BaseModel = str

    # params for operation using
    metadata: dict = field(default_factory=dict)
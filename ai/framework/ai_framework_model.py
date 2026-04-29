import uuid

from dataclasses import dataclass, field
from typing import Callable


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
    memory_short_disabled: bool = False
    memory_short_length: int = 10
    memory_session_uuid: str = str(uuid.uuid4())
    llm: str | None = None
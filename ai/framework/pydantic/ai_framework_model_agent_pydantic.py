from ai.framework.ai_framework_model_agent import AbstractAiFrameworkAgentModel


class AiFrameworkAgentModelPydantic(AbstractAiFrameworkAgentModel):
    def __init__(
            self,
            name: str,
            title: str,
            description: str,
            prompt: str,
            system_prompt: str,
            specialization: str | None,
            tools: list[str],
            mcp_servers: list[str],
            memory_short_disabled: bool,
            memory_short_length: int
    ):
        self.name = name
        self.title = title
        self.description = description
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.specialization = specialization
        self.tools = tools
        self.mcp_servers = mcp_servers
        self.memory_short_disabled = memory_short_disabled
        self.memory_short_length = memory_short_length

        self.response_model = str
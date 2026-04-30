from pydantic import BaseModel

from ai.framework.task.ai_framework_model import AbstractAiFrameworkTaskModel, AiTaskYaml


class AiFrameworkTaskModelPydantic(AbstractAiFrameworkTaskModel):
    def __init__(
            self,
            title: str,
            yaml: AiTaskYaml,
            sub_task_current_index: int,
            input_fields: list[str],
            tools: list[str],
            mcp_servers: list[str],
            response_model: BaseModel | str,

    ):
        super().__init__(title, yaml, sub_task_current_index, input_fields)
        self.tools = tools if tools is not None else []
        self.mcp_servers = mcp_servers
        self.response_model = response_model

    def input_values_get(self):
        return super().input_values_get()

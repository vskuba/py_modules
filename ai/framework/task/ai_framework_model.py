import uuid

from dataclasses import field, dataclass
from typing import Any, Callable, Optional
from pydantic import BaseModel

from ai.framework.ai_framework import AiFrameworkModel


class AiTaskYamlSubTask(BaseModel):
    name: str
    prompt: str
    inputs_default: dict[str, Any] = field(default_factory=dict)
    tools: Optional[list[str]] = field(default_factory=list)


class AiTaskYaml(BaseModel):
    name: str
    title: str
    history_save: bool = False
    mcp_servers: list[str] = field(default_factory=list)
    system_prompt: str
    sub_tasks: list[AiTaskYamlSubTask] = field(default_factory=list)


@dataclass
class AiSubTask:
    name: str
    prompt: str
    tools: list[Callable] = field(default_factory=list)


class AiFrameworkTaskModel(AiFrameworkModel):
    def __init__(
            self,
            title: str,
            yaml: AiTaskYaml,
            sub_task_current_index: int,
            input_fields: list[str]
    ):
        self.id = str(uuid.uuid4())
        self.title = title
        self.yaml: AiTaskYaml = yaml
        self.sub_task_current_index = sub_task_current_index
        self.input_fields = input_fields
        self.input_values: dict[str, str] = {}
        self.history_save: bool = False

    def input_values_set(self, values: dict[str, str]):
        for k in self.input_fields:
            if k not in values.keys():
                raise ValueError(
                    f'Input field "{k}" is required, but not found in input set values: "{values}"'
                )
        self.input_values = values
        # logger_info('Задача %s, содержит значения для параметров: %s' % (self.name, values))

    def input_values_get(self):
        for k in self.input_fields:
            if k not in self.input_values:
                self.input_values[k] = ''
        return self.input_values

import os
import traceback
from functools import partial

from ai.framework.ai_framework_manager_agent import AbstractAiFrameworkAgentManager
from ai.framework.ai_framework_model_agent import AiAgentYaml
from ai.framework.pydantic.ai_framework_model_agent_pydantic import AiFrameworkAgentModelPydantic
from config.config import config_get
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get


class AiFrameworkAgentManagerPydantic(AbstractAiFrameworkAgentManager):
    def dir(self) -> str:
        return config_get('data_dir') + '/' + config_get('agent_dir')

    def list(self) -> list[str]:
        if not os.path.exists(self.dir()):
            return []

        names = [os.path.splitext(f)[0] for f in os.listdir(self.dir()) if f.endswith('.yml')]

        return sorted(names)

    def filepath_get(self, name: str) -> str:
        if name not in self.list():
            raise ValueError("Agent with name '%s' not found" % name)

        return self.dir() + '/' + name + '.yml'

    def create(self, agent_yaml: AiAgentYaml) -> AiFrameworkAgentModelPydantic:
        try:

            # system_prompt = """
            #   ⚠️ STRICT API MODE:
            #   1. To provide the final answer, you MUST ALWAYS call the "final_result" tool.
            #   2. NO markdown formatting (no ```json ... ```), NO preamble, NO conversational text outside the JSON.
            #
            #   Example JSON final response:
            #   final_result({"text": "This is an example of your concise response here."})
            #
            #   """

            system_prompt = ''

            framework_model = AiFrameworkAgentModelPydantic(
                name=agent_yaml.name,
                title=agent_yaml.title,
                description=agent_yaml.description,
                prompt='',
                system_prompt=system_prompt + agent_yaml.system_prompt,
                tools=agent_yaml.tools if agent_yaml.tools else [],
                mcp_servers=agent_yaml.mcp_servers,
                specialization=agent_yaml.specialization,
                memory_short_disabled=agent_yaml.memory_short_disabled,
                memory_short_length=agent_yaml.memory_short_length
            )

            async def on_complete(response_text: str):
                queue_get('chat').put({"text": response_text, "who": 'agent'})

            framework_model.on_complete=partial(on_complete)

            return framework_model

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка при создании агента {agent_yaml.title}: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

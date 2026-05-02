import traceback

from functools import partial

from ai.framework.ai_framework import AbstractAiFrameworkManager
from ai.framework.agent.ai_framework_model import AiFrameworkAgentModel
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get
from src.repository.agent_specialization_repository import AgentSpecializationRepository


class AiFrameworkAgentManager(AbstractAiFrameworkManager):

    def create_framework_model(self, agent_entity: dict) -> AiFrameworkAgentModel:
        try:
            framework_model = AiFrameworkAgentModel(
                title=agent_entity.get('title', ''),
                description=agent_entity.get('description', ''),
                specialization=agent_entity.get('specialization_name', ''),
            )

            tools = []
            if agent_entity.get('tool'):
                tools = agent_entity['tool'].split(',')

            mcp_servers = []
            if agent_entity.get('mcp_server'):
                mcp_servers = agent_entity['mcp_server'].split(',')

            framework_model.name = agent_entity.get('name', '')
            framework_model.system_prompt = self.create_system_prompt(agent_entity)
            framework_model.tools = tools
            framework_model.mcp_servers = mcp_servers
            framework_model.session_disabled = agent_entity.get('memory_short_disabled', False)
            framework_model.entity = agent_entity

            async def on_complete(response_text: str):
                queue_get('chat').put({"text": response_text, "who": 'agent'})

            framework_model.on_complete = partial(on_complete)

            return framework_model

        except Exception as e:
            backtrace = traceback.format_exc()
            logger_info(
                f"❌ Ошибка при создании framework model агента {agent_entity.get('title')}: {e}."
                f"Полный стек вызовов:\n{backtrace}"
            )
            raise

    def create_system_prompt(self, agent_entity: dict, meta: dict = None) -> str:
        if not (specialization_id := agent_entity.get('specialization_id')):
            return agent_entity.get('system_prompt', '')

        repo = AgentSpecializationRepository()
        specialization_entity = repo.find_by_non_async({'id': specialization_id})

        if specialization_entity.get('name') == 'fact_extractor' \
                and meta \
                and (collection_name := meta.get('collection_name')):

            return agent_entity.get('system_prompt', '')

        return agent_entity.get('system_prompt', '')

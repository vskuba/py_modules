import traceback

from functools import partial

from ai.framework.ai_framework import AbstractAiFrameworkManager
from ai.framework.agent.ai_framework_model import AiFrameworkAgentModel
from logging_.logging_ import logger_info
from mysql_.mysql_table import mysql_table_metadata_get
from queue_.queue_ import queue_get
from src.mysql_.repository.agent_specialization_repository import AgentSpecializationRepository


class AiFrameworkAgentManager(AbstractAiFrameworkManager):

    async def create_framework_model(self, agent_entity: dict, metadata: dict = None) -> AiFrameworkAgentModel:
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
            framework_model.system_prompt = await self.create_system_prompt(agent_entity, metadata)
            framework_model.tools = tools
            framework_model.mcp_servers = mcp_servers
            framework_model.session_disabled = agent_entity.get('memory_short_disabled', False)
            framework_model.entity = agent_entity
            framework_model.metadata = metadata or {}

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

    async def create_system_prompt(self, agent_entity: dict, metadata: dict = None) -> str:
        system_prompt = agent_entity.get('system_prompt', '')

        if not (specialization_id := agent_entity.get('specialization_id', 0)):
            return system_prompt

        repo = AgentSpecializationRepository()
        specialization_entity = await repo.find(specialization_id)

        if specialization_entity.get('name') == 'fact_extractor' \
                and metadata \
                and (collection_name := metadata.get('collection_name')):

            collection_metadata = await self.collection_metadata_get(collection_name)

            example_fact_extract = ''
            if collection_metadata:
                example_fact_extract = "\n\nOUTPUT FORMAT EXAMPLE:\n"

                example_fact_extract += "\n[{\n"
                lines = [f'  "{f}": "' + v['description'] for f, v in collection_metadata.items()]
                example_fact_extract += ",\n".join(lines)
                example_fact_extract += "\n},{\n"
                example_fact_extract += ",\n".join(lines)
                example_fact_extract += "\n}]"

            return system_prompt + example_fact_extract

        return system_prompt

    async def collection_metadata_get(self, collection_name) -> dict:
        collection_metadata = {}

        for table_name in ['fact', f'fact_collection_{collection_name}']:
            table_metadata = await mysql_table_metadata_get(table_name)

            for field, m in table_metadata.items():
                comment_metadata = m.get('comment_metadata')
                if comment_metadata and comment_metadata.get('tag') == 'extract':
                    collection_metadata[field] = {
                        'table_name': table_name,
                        'description': m['comment_metadata']['desc'],
                    }

        return collection_metadata

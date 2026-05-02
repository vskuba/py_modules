import asyncio
import queue
from pathlib import Path

from ai.framework.agent.ai_framework_manager import AiFrameworkAgentManager
from ai.framework.ai_framework_model import AiFrameworkModel
from async_.async_ import async_waiting_start, async_waiting_clear
from config.config import config_get
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get
from src.repository.agent_repository import AgentRepository
from state.state import state_get

_agent_name_cache = None
_agent_desc_cache = None


async def agent_name_list() -> list[str]:
    """
    Returns a list of name all available AI agents in the system.
    """
    global _agent_name_cache
    if _agent_name_cache:
        return _agent_name_cache

    agents_dir = Path(config_get('data_dir')) / config_get('AGENT_DIR')

    if not agents_dir.exists():
        return []

    _agent_name_cache = [f.stem for f in agents_dir.glob("*.yml")]
    return _agent_name_cache


async def agent_desc_list() -> str:
    """
        Returns a list of all available AI agents in the system.
        Use this to discover which specialized agents can be called or assigned to tasks.
        Each agent in the list includes its name, title, and a description of its capabilities.
        """
    global _agent_desc_cache
    if _agent_desc_cache:
        return _agent_desc_cache

    result = ['Available agents:']

    repo = AgentRepository()
    agents = await repo.find_by({})

    for agent in agents:
        # ... ваша логика обработки ...
        a_name = agent.get('name').strip()
        a_desc = agent.get('description', '').strip()
        result.append(f'Agent name: "{a_name}", description: "{a_desc}"')

    _agent_desc_cache = '\n'.join(result)
    return _agent_desc_cache


async def agent_invoke(agent_name: str, prompt: str) -> str:
    """
    Executes a specific AI agent with a given prompt or task.
    Use this to delegate sub-tasks to specialized agents (e.g., 'researcher', 'coder').

    Args:
        agent_name: The unique identifier of the agent to call.
        prompt: Detailed instructions or question for the agent.

    Returns:
        The text response from the invoked agent after the task is completed.
    """
    agent_manager = AiFrameworkAgentManager()
    agent_names_list = await agent_name_list()

    if agent_name not in agent_names_list:
        return (f'Agent name "{agent_name}" not in the list of available agents.'
                f' Please check agent name and try again.'
                f' Support follow agent_name: [' + ', '.join(agent_names_list) + ']'
                )

    repo = AgentRepository()
    entity = await repo.find_one_by({"name": agent_name})

    if not entity:
        return f"Agent {agent_name} data not found in database."

    framework_model = agent_manager.create_framework_model(entity)
    framework_model.prompt = prompt

    framework_model_main_thread = state_get('framework_model_main_thread')
    if isinstance(framework_model_main_thread, AiFrameworkModel):
        framework_model.user_id = framework_model_main_thread.user_id

    async def on_complete(response_text):
        logger_info(f'Вызвана функция on_complete для агента {framework_model.name}')
        queue_get('chat').put({
            'text': f'Ответ от агента "{framework_model.title}": {response_text}',
            'who': 'agent'
        })
        queue_get('agent_response').put(response_text)

    framework_model.on_complete = on_complete
    framework_model.is_sub_agent = True

    queue_get('agent').put(framework_model)

    await async_waiting_start()

    try:
        while True:
            try:
                agent_response_text = queue_get('agent_response').get_nowait()
                break
            except queue.Empty:
                await asyncio.sleep(0.1)
    finally:
        async_waiting_clear()

    logger_info(f'Ответ от субагента {framework_model.name}: {agent_response_text}')

    return agent_response_text
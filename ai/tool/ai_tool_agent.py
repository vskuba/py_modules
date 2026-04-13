import asyncio
import queue

from async_.async_ import async_waiting_start, async_waiting_clear
from logging_.logging_ import logger_info
from queue_.queue_ import queue_get
from state.state import state_get


async def agent_list() -> str:
    """
    Returns a list of all available AI agents in the system.
    Use this to discover which specialized agents can be called or assigned to tasks.
    Each agent in the list includes its name, title, and a description of its capabilities.
    """
    result = ['Available agents:']
    for agent_model in state_get('agent_model_list'):
        if agent_model.name == 'researcher':
            continue

        result.append(
            f'Agent name: "{agent_model.name}", description: "{agent_model.description}"')

    return '\n'.join(result)


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
    if agent_name not in [a.name for a in state_get('agent_model_list')]:
        return (f'Agent name "{agent_name}" not in the list of available agents.'
                f' Please check agent name and try again.'
                f' Support follow agent_name: ['+', '.join([a.name for a in state_get('agent_model_list')]) + ']'
                )

    agent_model = None
    for a in state_get('agent_model_list'):
        if a.name == agent_name:
            agent_model = a
            break

    agent_model.prompt = prompt

    async def on_complete(response_text):
        logger_info(f'Вызвана функция on_complete для агента {agent_model.name}')
        queue_get()['chat'].put({
            'text': f'Ответ от агента "{agent_model.title}": {response_text}',
            'who': 'agent'
        })
        queue_get()['agent_response'].put(response_text)

    agent_model.on_complete = on_complete
    agent_model.is_sub_thread = True

    queue_get()['agent'].put(agent_model)

    await async_waiting_start()

    try:
        while True:
            try:
                agent_response_text = queue_get()['agent_response'].get_nowait()
                break
            except queue.Empty:
                await asyncio.sleep(0.1)
    finally:
        async_waiting_clear()

    logger_info(f'Ответ от субагента {agent_model.name}: {agent_response_text}')

    return agent_response_text
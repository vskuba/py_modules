import os
import re
import uuid

import httpx
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from pydantic import TypeAdapter

from pydantic_ai import Agent, ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.groq import GroqProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from ai.ai_session import ai_session_message_add
from ai.framework.ai_framework_model import AiFrameworkModel
from ai.tool.ai_tool import ai_tools_get, ai_tools_permanent_get
from config.config import config_get
from logging_.logging_ import log_request_body, logger_info, log_response_body

message_adapter = TypeAdapter(list[ModelMessage])


@dataclass
class AiFrameworkResult:
    text: str


class AgentRateLimitError(Exception):
    """Вызывается, когда AI-провайдер возвращает 429 (Rate Limit) или 413 (Context Window Exceeded)"""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class AbstractAiFramework(ABC):
    def __init__(self):
        self.uuid = str(uuid.uuid4()).split('-')[0]
        self.engine_storage: dict[str, Any] = {}
        self.message_history: dict[str, list[Any]] = {}

    @abstractmethod
    async def engine_prepare(self, framework_model: AiFrameworkModel) -> Agent:
        pass

    @abstractmethod
    async def engine_run(self, framework_model: AiFrameworkModel):
        pass

    @abstractmethod
    async def engine_result_handle(self, result, framework_model: AiFrameworkModel) -> AiFrameworkResult | None:
        model_name = framework_model.name
        if model_name not in self.message_history:
            self.message_history[model_name] = []
        self.message_history[model_name] = result.all_messages()
        pass

    @abstractmethod
    async def framework_run(self, framework_model: AiFrameworkModel):
        pass

    async def message_history_save(self, framework_model: AiFrameworkModel):
        if framework_model.session_disabled:
            return

        model_name = framework_model.name
        if model_name not in self.message_history:
            return

        logger_info('История сообщений: ' + str(self.message_history[model_name]))

        llm_id = framework_model.entity_agent.get('llm_id')

        for m in self.message_history[model_name]:
            tokens_count = None

            # 1. ЗАПРОСЫ (User, System, Tool Return)
            if hasattr(m, 'kind') and m.kind == 'request':
                for p in getattr(m, 'parts', []):
                    part_kind = getattr(p, 'part_kind', 'text')
                    content = getattr(p, 'content', '')

                    if not content and hasattr(p, 'result'):
                        content = str(p.result)

                    if not content:
                        continue

                    # Мы сохраняем только пользовательские запросы, остальные в workflow
                    if part_kind != 'user-prompt':
                        continue

                    # Определяем роль
                    # role = 'user'
                    # if part_kind == 'system-prompt':
                    #     role = 'system'
                    # elif part_kind == 'tool-return':
                    #     role = 'tool'
                    #     t_name = getattr(p, 'tool_name', 'unknown')
                    #     content = f"[{t_name}]: {content}"
                    # elif part_kind == 'user-prompt':

                    role = 'workflow'
                    part_kind = 'final-user-prompt'

                    # Используем твою функцию для добавления
                    await ai_session_message_add(
                        session_uuid=framework_model.session_uuid,
                        request_uuid=framework_model.request_uuid,
                        llm_id=llm_id,
                        user_id=framework_model.user_id,
                        role=role,
                        agent_id=framework_model.entity_agent.get('id'),
                        kind_type=part_kind,
                        content=str(content).strip(),
                        companion_id=framework_model.companion_id
                    )

            # 2. ОТВЕТЫ (Assistant, Thinking, Tool Calls)
            elif hasattr(m, 'kind') and m.kind == 'response':
                # Ассистент всегда имеет роль assistant
                role = 'llm'
                is_final = getattr(m, 'finish_reason', None) == 'stop'

                if hasattr(m, 'usage') and m.usage:
                    input_t = getattr(m.usage, 'input_tokens', 0) or 0
                    output_t = getattr(m.usage, 'output_tokens', 0) or 0
                    tokens_count = input_t + output_t

                if hasattr(m, 'parts'):
                    for part in m.parts:
                        p_type = str(type(part))

                        # Размышления
                        if "ThinkingPart" in p_type:
                            await ai_session_message_add(
                                session_uuid=framework_model.session_uuid,
                                request_uuid=framework_model.request_uuid,
                                llm_id=llm_id,
                                user_id=framework_model.user_id,
                                role=role,
                                agent_id=framework_model.entity_agent.get('id'),
                                kind_type='thinking',
                                content=part.content.strip(),
                                token=tokens_count,
                                companion_id=framework_model.companion_id
                            )

                        # Текст ответа (Финальный или промежуточный)
                        elif "TextPart" in p_type:
                            kind = 'response-final' if is_final and not framework_model.is_transition else 'response'
                            await ai_session_message_add(
                                session_uuid=framework_model.session_uuid,
                                request_uuid=framework_model.request_uuid,
                                llm_id=llm_id,
                                user_id=framework_model.user_id,
                                role=role,
                                agent_id=framework_model.entity_agent.get('id'),
                                kind_type=kind,
                                content=part.content.strip(),
                                token=tokens_count,
                                companion_id=framework_model.companion_id
                            )

                        # Вызовы инструментов
                        elif "ToolCallPart" in p_type:
                            await ai_session_message_add(
                                session_uuid=framework_model.session_uuid,
                                request_uuid=framework_model.request_uuid,
                                llm_id=llm_id,
                                user_id=framework_model.user_id,
                                role=role,
                                agent_id=framework_model.entity_agent.get('id'),
                                kind_type='tool-call',
                                content=part.content.strip(),
                                token=tokens_count,
                                companion_id=framework_model.companion_id
                            )

        self.message_history = {}

    def engine_tools_local_add(self, engine, framework_model: AiFrameworkModel):
        tools_local = ai_tools_get()
        tools_mapping = {func.__name__: func for func in tools_local}
        tools_local_added = []
        for t_name in framework_model.tools:
            if t_name in tools_mapping:
                engine.tool_plain(tools_mapping[t_name])
                tools_local_added.append(t_name)

        ai_tools_permanent = ai_tools_permanent_get()
        for t in ai_tools_permanent:
            if t.__name__ not in tools_mapping:
                engine.tool_plain(t)
                tools_local_added.append(t.__name__)

        logger_info('🛠 Local инструменты добавлены: ' + ', '.join(tools_local_added))

    def llm_model_get(self, framework_model: AiFrameworkModel) -> Model:
        model_name = config_get('llm')
        if framework_model.entity_llm_current:
            model_name = framework_model.entity_llm_current.get('name')

        model = None

        http_client = httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(60.0, connect=10.0),
            event_hooks={
                'request': [log_request_body],
                'response': [log_response_body]
            }
        )

        if model_name.lower().startswith('openrouter/'):
            provider = OpenRouterProvider(
                app_url=config_get('OPENROUTER_API_URL'),
                api_key=config_get('OPENROUTER_API_KEY'),
                http_client=http_client
            )
            model_name = model_name.replace('openrouter/', '')
            model = OpenRouterModel(model_name, provider=provider)

        if model_name.lower().startswith('claude'):
            provider = AnthropicProvider(
                api_key=config_get('ANTHROPIC_API_KEY'),
                http_client=http_client
            )
            model = AnthropicModel(model_name, provider=provider)

        if model_name.lower().startswith('ollama'):
            provider = OpenAIProvider(
                base_url='http://host.docker.internal:11434/v1',
                api_key='ollama',
                http_client=http_client
            )
            model_name = re.sub(r'^[^/]*/', '', model_name)
            model = OpenAIChatModel(
                model_name=model_name,
                provider=provider
            )

        if model_name.lower().startswith('gemini/'):
            provider = GoogleProvider(api_key=config_get('GEMINI_API_KEY'))
            model_name = model_name.replace('gemini/', '')
            model = GoogleModel(
                model_name=model_name,
                provider=provider
            )

        if model_name.lower().startswith('groq/'):
            provider = GroqProvider(
                api_key=os.getenv('GROQ_API_KEY'),
                base_url='https://api.groq.com',
                http_client=http_client
            )
            model_name = model_name.replace('groq/', '')
            model = GroqModel(
                model_name=model_name,
                provider=provider,
            )

        if model_name.lower().startswith('grok'):
            provider = OpenAIProvider(
                base_url='https://api.x.ai/v1',
                api_key=os.getenv('XAI_API_KEY'),
                http_client=http_client  # Твой клиент с хуками теперь БУДЕТ работать
            )
            model = OpenAIChatModel(model_name=model_name, provider=provider)

        if not model:
            model = OpenAIChatModel(model_name)

        logger_info(f'🧠 LLM модель: {model_name} ({type(model).__name__})')

        return model

import uuid

import httpx
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from pydantic import TypeAdapter

from pydantic_ai import Agent, ModelMessage, ModelSettings
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from ai.framework.ai_framework_model import AiFrameworkModel
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


class LlmProviderError(Exception):
    """Вызывается, когда AI-провайдер возвращает некорректный ответ
    (не-JSON вместо JSON, UnexpectedModelBehavior и т.п.)"""

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
                    await self.session_message_add(
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
                            await self.session_message_add(
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
                            await self.session_message_add(
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
                            tool_content = getattr(part, 'content', '')
                            await self.session_message_add(
                                session_uuid=framework_model.session_uuid,
                                request_uuid=framework_model.request_uuid,
                                llm_id=llm_id,
                                user_id=framework_model.user_id,
                                role=role,
                                agent_id=framework_model.entity_agent.get('id'),
                                kind_type='tool-call',
                                content=tool_content,
                                token=tokens_count,
                                companion_id=framework_model.companion_id
                            )

        self.message_history = {}

    @staticmethod
    def llm_model_get(framework_model: AiFrameworkModel) -> Model:
        model_name = config_get('llm')
        if framework_model.entity_llm_current:
            model_name: str = framework_model.entity_llm_current.get('name', '')

        model = None

        http_client = httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(60.0, connect=10.0),
            event_hooks={
                'request': [log_request_body],
                'response': [log_response_body]
            }
        )

        mapping_providers = {
            'openrouter',
            'claude',
            'ollama',
            'gemini',
            'groq',
            'mistral',
            'huggingface',
            'cerebras',
            'openai',
            'gx10',
        }

        thinking = framework_model.thinking

        for i in mapping_providers:
            if model_name.lower().startswith(i + '/'):
                model_name_clean = model_name[len(i) + 1:]

                if i != 'gx10':
                    provider = OpenAIProvider(
                        base_url=config_get(i.upper() + '_API_URL'),
                        api_key=config_get(i.upper() + '_API_KEY'),
                        http_client=http_client
                    )
                    model = OpenAIChatModel(model_name_clean, provider=provider)
                    # Пересобираем с настройками: чем именно глушить размышления,
                    # зависит от профиля модели, а он есть только у собранной.
                    if not thinking:
                        model = OpenAIChatModel(model_name_clean, provider=provider,
                                                settings=_settings_thinking_off(model))

                if i == 'gx10':
                    model_size = model_name_clean.split('-')[-1].upper()
                    provider = OpenAIProvider(
                        base_url=config_get(i.upper() + f'_{model_size}_API_URL'),
                        api_key=config_get(i.upper() + '_API_KEY'),
                        http_client=http_client
                    )
                    model = OpenAIChatModel(
                        model_name_clean,
                        provider=provider,
                        # llama.cpp переключает размышления аргументом шаблона чата,
                        # а не полем запроса: профиля у неё в pydantic-ai нет, и
                        # единый ключ до неё не доехал бы. Передаём оба значения —
                        # у шаблона своё умолчание, и «не передали» означает не
                        # «выключено», а «как решит модель».
                        settings=ModelSettings(
                            extra_body={'chat_template_kwargs': {'enable_thinking': thinking}})
                    )

                if i == 'gemini':
                    provider = GoogleProvider(
                        base_url=config_get(i.upper() + '_API_URL'),
                        api_key=config_get(i.upper() + '_API_KEY'),
                        http_client=http_client)
                    model = GoogleModel(model_name_clean, provider=provider)
                    if not thinking:
                        model = GoogleModel(model_name_clean, provider=provider,
                                            settings=_settings_thinking_off(model))

                break

        if not model:
            raise ValueError(f"Для LLM модели '{model_name}' не найден подходящий провайдер. Проверьте конфигурацию.")

        logger_info(f'🧠 LLM модель: {model_name} ({type(model).__name__}), '
                    f'размышления: {"да" if thinking else "нет"}')

        return model

    @abstractmethod
    async def session_message_add(
            self,
            session_uuid,
            request_uuid,
            llm_id,
            user_id,
            companion_id,
            role,
            agent_id,
            kind_type,
            content,
            token=None
    ):
        pass


def _settings_thinking_off(model: Model) -> ModelSettings:
    """
    Настройки, глушащие размышления модели.

    Ключ `thinking` — единый: библиотека сама переводит его в `reasoning_effort`
    у OpenAI и в `thinking_budget=0` у Google, а модели, которая размышлять не
    умеет, не передаёт ничего. Разбирать провайдеров руками тут не нужно и вредно —
    свой разбор устарел бы на следующей модели.

    Значение зависит от модели. `False` у тех, кто размышляет всегда (o-серия,
    gpt-5, gpt-oss через openrouter), pydantic-ai **молча отбрасывает**: выключить
    у них нечего, и запрет не доехал бы до запроса вовсе. Для них ближайшее к
    запрету — самый низкий уровень усилий. Проверено по телу запроса: `False` даёт
    пустое тело, `'minimal'` — `reasoning_effort: minimal`.

    Ключа нет в pydantic-ai до 2.x — там возвращаем пустые настройки: запрет молча
    не применяется, и это лучше отказа, единственная настройка модели не должна
    ронять весь вызов. Форма профиля у версий тоже разная — объект против словаря.
    """
    if 'thinking' not in getattr(ModelSettings, '__annotations__', {}):
        return ModelSettings()

    profile = getattr(model, 'profile', None)
    always = (profile.get('thinking_always_enabled', False) if isinstance(profile, dict)
              else getattr(profile, 'thinking_always_enabled', False))

    return ModelSettings(thinking='minimal' if always else False)

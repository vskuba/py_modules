import os
import re
import uuid
from pathlib import Path

import httpx
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
from pydantic import TypeAdapter

from pydantic_ai import Agent, ModelRequest, ModelResponse, ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.groq import GroqProvider
from pydantic_ai.providers.openai import OpenAIProvider

from ai.ai_memory_short import memory_short_message_add, memory_short_messages
from ai.tool.ai_tool import ai_tools_get, ai_tools_permanent_get
from config.config import config_get
from logging_.logging_ import log_request_body, logger_info, log_response_body
from state.state import state_set

message_adapter = TypeAdapter(list[ModelMessage])


@dataclass
class AiFrameworkResult:
    text: str


@dataclass
class AiFrameworkModel:
    name: str
    prompt: str
    system_prompt: str
    user_id: int
    tools: list[Callable] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    on_complete: Callable | None = None
    is_sub_thread: bool = False
    is_gui_mode: bool = True
    memory_short_disabled: bool = False
    memory_short_length: int = 10


@dataclass
class AbstractAiFrameworkManager(ABC):
    @abstractmethod
    def load(self, name: str) -> AiFrameworkModel:
        pass


class AbstractAiFramework(ABC):
    def __init__(self, framework_manager):
        self.uuid = str(uuid.uuid4()).split('-')[0]
        self.framework_manager: AbstractAiFrameworkManager = framework_manager
        self.engine_storage: dict[str, Any] = {}
        self.message_history: dict[str, list[Any]] = {}

    @abstractmethod
    def engine_prepare(self, framework_model: AiFrameworkModel) -> Agent:
        pass

    @abstractmethod
    async def engine_run(self, framework_model: AiFrameworkModel):
        pass

    @abstractmethod
    async def engine_result_handle(self, result, framework_model: AiFrameworkModel) -> AiFrameworkResult | None:
        # save short memory
        messages_new = result.new_messages()
        if messages_new:
            output_message = []
            if not isinstance(result.output, str):
                output_message = [result.output]
            await self.message_history_add(framework_model, messages_new + output_message)

        # create report
        model_name = framework_model.name
        if model_name not in self.message_history:
            self.message_history[model_name] = []
        self.message_history[model_name] += result.all_messages()

        pass

    @abstractmethod
    async def framework_run(self, framework_model: AiFrameworkModel):
        pass

    async def message_history_get(self, framework_model: AiFrameworkModel) -> list[Any] | None:
        if framework_model.memory_short_disabled:
            return None

        all_messages = []
        for msg_json in await memory_short_messages(
                framework_model.user_id,
                framework_model.name,
                framework_model.memory_short_length
        ):
            msg_obj_list = message_adapter.validate_json(msg_json)
            all_messages.extend(msg_obj_list)

        return all_messages

    async def message_history_add(self, framework_model: AiFrameworkModel, messages_new: list[Any]):
        if framework_model.memory_short_disabled:
            return None

        for msg in messages_new:
            if not isinstance(msg, (ModelRequest, ModelResponse)):
                continue

            role = 'user'
            if not isinstance(msg, ModelRequest):
                role = 'assistant'

            msg_json = message_adapter.dump_json([msg]).decode('utf-8')
            await memory_short_message_add(
                framework_model.user_id,
                role,
                framework_model.name,
                msg_json
            )

    def llm_model_get(self, framework_model: AiFrameworkModel) -> Model:
        model_name = config_get('llm')
        model = None

        http_client = httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(60.0, connect=10.0),
            event_hooks={
                'request': [log_request_body],
                'response': [log_response_body]
            }
        )

        if model_name.lower().startswith('claude'):
            provider = AnthropicProvider(
                api_key=config_get('ANTHROPIC_API_KEY'),
                http_client=http_client
            )
            model = AnthropicModel(model_name, provider=provider)

        if model_name.lower().startswith('ollama'):
            provider = OpenAIProvider(
                base_url='http://localhost:11434/v1',
                api_key='ollama',
                http_client=http_client
            )
            model_name = re.sub(r'^[^/]*/', '', model_name)
            model = OpenAIChatModel(
                model_name=model_name,
                provider=provider
            )

        if model_name.lower().startswith('gemini'):
            model = GoogleModel(
                model_name=model_name.lower()
            )

        if model_name.lower().startswith('groq'):
            model_name = re.sub(r'^groq:', '', model_name, flags=re.IGNORECASE)

            provider = GroqProvider(
                api_key=os.getenv('GROQ_API_KEY'),
                base_url='https://api.groq.com',
                http_client=http_client
            )
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

        return model

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

    async def framework_report(self, framework_model: AiFrameworkModel):
        report = []
        total_tokens = 0

        if self.message_history:
            for agent_name, messages in self.message_history.items():
                report.append(f"=== АГЕНТ: {agent_name} ===\n")

                for m in messages:
                    role = '❓'
                    parts = []

                    # 1. ЗАПРОС (System / User)
                    if hasattr(m, 'kind') and m.kind == 'request':
                        role = '👤'
                        if hasattr(m, 'parts'):
                            for p in m.parts:
                                content = getattr(p, 'content', '')
                                if not content: continue

                                p_type = str(type(p))
                                if "SystemPromptPart" in p_type:
                                    role = '⚙️'
                                elif "ToolReturnPart" in p_type or "ToolResult" in p_type:
                                    role = '🔧'  # Меняем иконку на ключ для ответов инструментов
                                    content = f"РЕЗУЛЬТАТ: {content}"

                                parts.append(content)

                    # 2. ОТВЕТ (Assistant / AI)
                    elif hasattr(m, 'kind') and m.kind == 'response':
                        role = '🧠'
                        thinking = getattr(m, 'thinking', None)
                        if thinking:
                            parts.append(f"<think>\n{thinking}\n</think>")

                        text = getattr(m, 'text', None)
                        if text: parts.append(text)

                        # Вызовы инструментов (Assistant говорит: "Я хочу вызвать...")
                        tool_calls = getattr(m, 'tool_calls', [])
                        if tool_calls:
                            role = '🧠'  # Оставляем иконку мозга, так как это намерение AI
                            for tc in tool_calls:
                                name = getattr(tc, 'tool_name', 'unknown')
                                args = getattr(tc, 'args', '{}')
                                parts.append(f"🔧 ВЫЗОВ: {name}({args})")

                        # ИЗВЛЕКАЕМ ТОКЕНЫ
                        usage = getattr(m, 'usage', None)
                        if usage:
                            # Суммируем для заголовка отчета
                            total_tokens += getattr(usage, 'total_tokens', 0)

                            # Можно добавить инфо о токенах прямо в replica для этого сообщения
                            tokens_info = f"[Tokens: {usage.request_tokens} -> {usage.response_tokens}]"
                            parts.append(tokens_info)

                    # 3. ОТВЕТ ИНСТРУМЕНТА (Результат выполнения)
                    # Проверяем по имени класса или атрибутам
                    elif "ToolReturn" in str(type(m)) or "ToolResult" in str(type(m)):
                        role = '🔧'  # Тот самый значок для ответа
                        # Извлекаем результат (обычно в поле content или result)
                        content = getattr(m, 'content', '') or getattr(m, 'result', '')
                        if content:
                            parts.append(f"РЕЗУЛЬТАТ: {str(content)}")

                    elif "ResponseModel" in str(type(m)):
                        role = "🎯"  # Иконка цели/финиша
                        # Согласно скриншоту, текст лежит прямо в атрибуте 'text'
                        final_text = getattr(m, 'text', '')
                        if final_text:
                            # Чистим от спец-пробелов для красоты
                            final_text = final_text.replace('\u202f', ' ')
                            parts.append(f"ИТОГОВЫЙ ОТВЕТ: {final_text}")

                    # Если сообщение пустое (техническое), не добавляем его в отчет
                    if not parts:
                        continue

                    string_parts = []
                    for p in parts:
                        if isinstance(p, list):
                            string_parts.append(" ".join(map(str, p)))
                        else:
                            string_parts.append(str(p))

                    replica = "\n\n".join(string_parts)
                    report.append(f"{role}: {replica}\n")

            filename = f"{framework_model.name}_{self.uuid}.txt"

            # Собираем путь
            path = Path(config_get('data_dir')) / config_get('report_dir') / filename
            path.parent.mkdir(parents=True, exist_ok=True)

            # Добавляем заголовок в самое начало списка
            header = f"👤-🧠 Текущая история общения: {'(total_tokens: ' + str(total_tokens) + ')' if total_tokens > 0 else ''}\n"
            report.insert(0, header)
            report.insert(1, "=" * 50 + "\n")

            # Записываем всё в файл (используем 'w', так как имя файла уникальное по времени)
            with open(path, 'w', encoding='utf-8') as f:
                # Добавляем пару пустых строк перед новым отчетом для визуального разделения
                f.write("\n\n" + "=" * 60 + "\n")
                f.write('\n'.join(report) + "\n")

            print(f"✅ Отчет сохранен: {path}")
            state_set('last_report_path', path)

"""
Сервисы, говорящие по протоколу OpenAI: сам OpenAI и совместимые с ним.

Один класс на всех, потому что различаются они только адресом и ключом — и то и
другое лежит в окружении под именем сервиса (`GROQ_API_URL`, `GROQ_API_KEY`).
Поведение же одинаково вплоть до формата ответа, ради этого совместимость и
делается.

Сервис, у которого появится своя особенность, отсюда уезжает в собственный файл —
так уже случилось с OpenRouter (`ai_provider_openrouter.py`) и с локальной моделью
(`ai_provider_gx10.py`).
"""
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ai.framework.ai_framework_model import AiFrameworkModel
from ai.provider.ai_provider import AiProvider
from config.config import config_get


class AiProviderOpenai(AiProvider):
    """Сервис по протоколу OpenAI. Адрес и ключ — из окружения по имени сервиса."""

    name = 'openai'

    def model_get(self, model_name: str, framework_model: AiFrameworkModel) -> Model:
        provider = OpenAIProvider(
            base_url=config_get(self.name.upper() + '_API_URL'),
            api_key=config_get(self.name.upper() + '_API_KEY'),
            http_client=self.http_client,
        )
        model = OpenAIChatModel(model_name, provider=provider)
        if not framework_model.thinking_disabled:
            return model

        # Пересобираем с настройками: чем именно глушить размышления, зависит от
        # профиля модели, а он есть только у собранной.
        return OpenAIChatModel(model_name, provider=provider,
                               settings=self.thinking_settings(model))


class AiProviderClaude(AiProviderOpenai):
    name = 'claude'


class AiProviderOllama(AiProviderOpenai):
    name = 'ollama'


class AiProviderGroq(AiProviderOpenai):
    name = 'groq'


class AiProviderMistral(AiProviderOpenai):
    name = 'mistral'


class AiProviderHuggingface(AiProviderOpenai):
    name = 'huggingface'


class AiProviderCerebras(AiProviderOpenai):
    name = 'cerebras'

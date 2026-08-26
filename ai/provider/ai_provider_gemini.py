"""
Gemini — единственный сервис не по протоколу OpenAI.

У него свой класс модели (`GoogleModel`) и свой провайдер: протокол Google, а не
OpenAI, и подменой адреса это не обходится.

Размышления при этом глушатся тем же единым ключом, что и у соседей: pydantic-ai
сама переводит его в `thinking_budget=0`. Разбирать это здесь не нужно — тем и
хорош общий ключ.
"""
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from ai.framework.ai_framework_model import AiFrameworkModel
from ai.provider.ai_provider import AiProvider
from config.config import config_get


class AiProviderGemini(AiProvider):
    name = 'gemini'

    def model_get(self, model_name: str, framework_model: AiFrameworkModel) -> Model:
        provider = GoogleProvider(
            base_url=config_get(self.name.upper() + '_API_URL'),
            api_key=config_get(self.name.upper() + '_API_KEY'),
            http_client=self.http_client,
        )
        model = GoogleModel(model_name, provider=provider)
        if not framework_model.thinking_disabled:
            return model

        return GoogleModel(model_name, provider=provider,
                           settings=self.thinking_settings(model))

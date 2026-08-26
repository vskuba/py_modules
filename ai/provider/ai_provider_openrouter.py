"""
OpenRouter — маршрутизатор к чужим моделям.

Свой файл появился не ради порядка, а из-за дефекта. Собранный общим
`OpenAIProvider` с подменённым адресом, OpenRouter для pydantic-ai — «какой-то
совместимый сервер», и профиль модели она берёт общий. В нём `deepseek-v4-flash`
помечен неразмышляющим, а настройку `thinking` таким моделям библиотека **молча
выбрасывает**: запрет размышлений не доезжал до запроса вовсе, тело уходило
пустым, хотя флаг в базе стоял.

`OpenRouterProvider` знает каталог моделей маршрутизатора, и с ним тот же запрет
превращается в `reasoning_effort`. Адрес он берёт свой, поэтому `OPENROUTER_API_URL`
здесь не нужен — ключ тот же.

Имя модели у него двухуровневое: `openrouter/deepseek/deepseek-v4-flash-0731`. До
первого слэша — сам маршрутизатор, дальше — имя у него, вместе с автором модели.
"""
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from ai.framework.ai_framework_model import AiFrameworkModel
from ai.provider.ai_provider import AiProvider
from config.config import config_get


class AiProviderOpenrouter(AiProvider):
    name = 'openrouter'

    def model_get(self, model_name: str, framework_model: AiFrameworkModel) -> Model:
        provider = OpenRouterProvider(
            api_key=config_get(self.name.upper() + '_API_KEY'),
            http_client=self.http_client,
        )
        model = OpenAIChatModel(model_name, provider=provider)
        if not framework_model.thinking_disabled:
            return model

        return OpenAIChatModel(model_name, provider=provider,
                               settings=self.thinking_settings(model))

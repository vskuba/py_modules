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

    def raw_endpoint(self, model_name: str) -> tuple[str, str]:
        """
        Адрес по общему соглашению; пустой `OPENROUTER_API_URL` — не беда.

        Для сборки модели адрес не нужен вовсе (`OpenRouterProvider` знает его сам),
        но сырому запросу деваться некуда: он идёт обычным HTTP, и адрес назвать
        обязан. Соглашение то же, что у соседей, — переопределение здесь только
        ради этой оговорки.
        """
        return super().raw_endpoint(model_name)

    def raw_body_no_thinking(self) -> dict:
        """
        У маршрутизатора размышления гасит собственное поле `reasoning`.

        Не `reasoning_effort`: в него единый ключ переводит уже pydantic-ai, а сырой
        запрос идёт мимо неё. Проверено живьём на `qwen/qwen3.7-flash`: без поля
        ответ пришёл с `content: null` и полным `reasoning`, с полем — «Pong» за
        820 мс.

        `{'max_tokens': 0}` вместо `enabled` не годится — маршрутизатор рвёт
        соединение, не отвечая вовсе.
        """
        return {'reasoning': {'enabled': False}}

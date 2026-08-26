"""
Выбор стратегии по имени модели и сборка модели ею.

Единственная точка, которая знает обо всех сервисах разом. Всё, что различается
между ними, лежит в их файлах; здесь только соответствие «префикс имени — класс».

Реестр перечислен явно, а не собирается обходом модуля. Список сервисов — часть
контракта: по нему видно, что вообще поддерживается, а имя незнакомого сервиса
должно давать внятный отказ, а не молчаливый пропуск.
"""
import httpx

from pydantic_ai.models import Model

from ai.framework.ai_framework_model import AiFrameworkModel
from ai.provider.ai_provider import AiProvider
from ai.provider.ai_provider_gemini import AiProviderGemini
from ai.provider.ai_provider_gx10 import AiProviderGx10
from ai.provider.ai_provider_openai import (AiProviderCerebras, AiProviderClaude,
                                            AiProviderGroq, AiProviderHuggingface,
                                            AiProviderMistral, AiProviderOllama,
                                            AiProviderOpenai)
from ai.provider.ai_provider_openrouter import AiProviderOpenrouter

# Все стратегии. Ключ — то, что стоит до первого слэша в имени модели.
AI_PROVIDER_REGISTRY: dict[str, type[AiProvider]] = {
    provider.name: provider for provider in (
        AiProviderOpenrouter,
        AiProviderGx10,
        AiProviderGemini,
        AiProviderOpenai,
        AiProviderClaude,
        AiProviderOllama,
        AiProviderGroq,
        AiProviderMistral,
        AiProviderHuggingface,
        AiProviderCerebras,
    )
}


def ai_provider_registry_model_get(model_name: str, framework_model: AiFrameworkModel,
                                   http_client: httpx.AsyncClient) -> Model:
    """
    Собрать модель стратегией её сервиса.

    Args:
        model_name: полное имя с префиксом сервиса — `openrouter/deepseek/…`.
        framework_model: ход целиком; из него стратегия берёт настройки размышлений.
        http_client: общий клиент с журналированием запросов.

    Raises:
        ValueError: имя без известного префикса. Отказываем сразу и по имени: без
            этого вызов уходил бы в никуда и падал позже, уже на ответе провайдера.
    """
    provider_name, _, model_name_clean = str(model_name or '').partition('/')
    provider_class = AI_PROVIDER_REGISTRY.get(provider_name.lower())

    if not provider_class or not model_name_clean:
        raise ValueError(
            f"Для LLM модели '{model_name}' не найден подходящий провайдер. "
            f"Известные: {', '.join(sorted(AI_PROVIDER_REGISTRY))}. Проверьте конфигурацию.")

    return provider_class(http_client).model_get(model_name_clean, framework_model)

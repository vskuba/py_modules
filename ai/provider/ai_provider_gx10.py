"""
GX10 — локальная модель на llama.cpp.

Две особенности, и обе не сводятся к общему случаю.

**Адрес зависит от размера.** Модели крутятся на разных портах одной машины, и
какой брать, видно из хвоста имени: `gx10/qwen-medium` -> `GX10_MEDIUM_API_URL`.
У остальных сервисов адрес один на все модели.

**Размышления переключаются не полем запроса, а аргументом шаблона чата** —
`chat_template_kwargs.enable_thinking`. Профиля этой модели в pydantic-ai нет, и
единый ключ `thinking` до неё не доехал бы: библиотека сочла бы, что модель
размышлять не умеет, и убрала настройку.

Отсюда же и третье отличие: значение передаём всегда, обе стороны. У шаблона своё
умолчание, и «не передали» здесь означает не «выключено», а «как решит модель».
Поэтому смотрим на `thinking` — просьбу шага с учётом запрета, — а не на один
только запрет, как у соседей.

Практический повод: с включёнными размышлениями эта модель тратит на внутренний
монолог весь бюджет ответа и возвращает пустой `content`. По той же причине
`enable_thinking: false` зашит в `~/.claude/bin/gx10`.
"""
from pydantic_ai import ModelSettings
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ai.framework.ai_framework_model import AiFrameworkModel
from ai.provider.ai_provider import AiProvider
from config.config import config_get


class AiProviderGx10(AiProvider):
    name = 'gx10'

    def model_get(self, model_name: str, framework_model: AiFrameworkModel) -> Model:
        provider = OpenAIProvider(
            base_url=config_get(f'{self.name.upper()}_{self._size(model_name)}_API_URL'),
            api_key=config_get(self.name.upper() + '_API_KEY'),
            http_client=self.http_client,
        )

        return OpenAIChatModel(
            model_name,
            provider=provider,
            settings=ModelSettings(extra_body={
                'chat_template_kwargs': {'enable_thinking': framework_model.thinking},
            }),
        )

    @staticmethod
    def _size(model_name: str) -> str:
        """Размер модели из хвоста имени: `qwen-medium` -> `MEDIUM`, он же часть адреса."""
        return model_name.split('-')[-1].upper()

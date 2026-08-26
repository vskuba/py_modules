"""
Провайдер LLM: как из имени модели собрать объект, которым говорят с сервисом.

Зачем стратегии. Сборка была одной функцией с цепочкой `if` по имени сервиса, и
каждая особенность оседала там же: у локальной модели свой ключ размышлений, у
Gemini другой класс модели, у OpenRouter свой провайдер pydantic-ai, у gx10 адрес
зависит от размера модели. Ветки чужих сервисов при этом стояли рядом и читались
как общая логика — так и появился дефект, из-за которого запрет размышлений не
доезжал до моделей за OpenRouter: их собирали общим провайдером, а он подсовывает
чужой профиль модели.

Теперь особенность каждого сервиса живёт в своём файле и не мешается с соседями.
Новый сервис — новый файл, а не ещё одна ветка в общей функции.

Имя модели несёт и сервис: `openrouter/deepseek/deepseek-v4-flash-0731`. До первого
слэша — кто её обслуживает, дальше — как она называется у него. Разбор общий, он
здесь; всё остальное решает конкретная стратегия.
"""
from abc import ABC, abstractmethod

import httpx
from pydantic_ai import ModelSettings
from pydantic_ai.models import Model

from ai.framework.ai_framework_model import AiFrameworkModel


class AiProvider(ABC):
    """
    Стратегия одного сервиса LLM.

    Наследник обязан объявить `name` — то, что стоит до слэша в имени модели, — и
    собрать модель в `model_get`. Всё остальное (клиент HTTP, разбор имени, запрет
    размышлений) даётся готовым.
    """

    # Ключ сервиса в имени модели и префикс его переменных окружения:
    # `openrouter` -> `OPENROUTER_API_KEY`, `OPENROUTER_API_URL`.
    name: str = ''

    def __init__(self, http_client: httpx.AsyncClient):
        self.http_client = http_client

    @abstractmethod
    def model_get(self, model_name: str, framework_model: AiFrameworkModel) -> Model:
        """
        Собрать модель.

        Args:
            model_name: имя без префикса сервиса — то, как модель зовут у него.
            framework_model: ход целиком; из него берутся настройки размышлений.
        """

    @staticmethod
    def thinking_settings(model: Model) -> ModelSettings:
        """
        Настройки, глушащие размышления уже собранной модели.

        Ключ `thinking` — единый: pydantic-ai сама переводит его в `reasoning_effort`
        у OpenAI-совместимых и в `thinking_budget=0` у Google, а модели, которая
        размышлять не умеет, не передаёт ничего. Разбирать сервисы руками тут не
        нужно и вредно — свой разбор устарел бы на следующей модели.

        Значение зависит от модели. `False` у тех, кто размышляет всегда (o-серия,
        gpt-5, gpt-oss), pydantic-ai **молча отбрасывает**: выключить у них нечего,
        и запрет не доехал бы до запроса вовсе. Для них ближайшее к запрету — самый
        низкий уровень усилий.

        Профиль читается только у собранной модели, поэтому её и приходится
        собирать дважды: конструктор ничего не запрашивает по сети, это дёшево.

        Ключа нет в pydantic-ai до 2.x — там возвращаем пустые настройки: запрет
        молча не применяется, и это лучше отказа, одна настройка модели не должна
        ронять весь вызов. Форма профиля у версий тоже разная — объект против
        словаря, читаем обе.
        """
        if 'thinking' not in getattr(ModelSettings, '__annotations__', {}):
            return ModelSettings()

        profile = getattr(model, 'profile', None)
        always = (profile.get('thinking_always_enabled', False) if isinstance(profile, dict)
                  else getattr(profile, 'thinking_always_enabled', False))

        return ModelSettings(thinking='minimal' if always else False)

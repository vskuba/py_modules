"""
GX10 — локальная модель на vLLM (раньше сервером был llama.cpp).

Три особенности, и все не сводятся к общему случаю.

**Сервер строг к имени модели.** vLLM знает только имя, под которым поднял
модель, — `qwen3.8-flash-next` (`GX10_MODEL` в `~/.claude/gx10.env`); на
реестровый хвост `qwen-large` отвечает 404, которого llama.cpp не показывал
вовсе — он принимал любое. Отсюда `raw_model`: в тело запроса идёт
`GX10_<РАЗМЕР>_MODEL`, иначе `GX10_MODEL`, иначе имя из реестра как есть.
Адрес по-прежнему на каждый размер свой (`GX10_LARGE_API_URL`,
`GX10_VISION_API_URL`), ключ один — `GX10_API_KEY`.

**Размышления переключаются не полем запроса, а аргументом шаблона чата** —
`chat_template_kwargs.enable_thinking`. Шаблон Qwen под vLLM его понимает:
проверено живьём — с `false` ответ непустой и `reasoning` пуст, с `true`
модель размышляет (на малом бюджете — `content: null` при непустом `reasoning`).
Профиля этой модели в pydantic-ai нет, и единый ключ `thinking` до неё не
доехал бы: библиотека сочла бы, что модель размышлять не умеет, и убрала
настройку.

Отсюда же правило значения: передаём всегда, обе стороны. У шаблона своё
умолчание, и «не передали» здесь означает не «выключено», а «как решит модель».
Поэтому смотрим на `thinking` — просьбу шага с учётом запрета, — а не на один
только запрет, как у соседей.

**Грамматика ответа (GBNF) с переездом уснула, но не мешает.** Поле `grammar`
в теле запроса — поле llama.cpp; в pydantic-ai поля такого нет, оно едет в
`extra_body`. vLLM незнакомого поля не отвергает (проверено живьём: ответ 200),
но и не применяет — молча игнорирует: с грамматикой, разрешающей одно «pong»,
модель вернула «Pong». Ошибки от этого нет, но и ограничением ответ быть
перестал; переезд грамматик на серверную схему — отдельная задача. С
грамматикой дополнительно гасится thinking: reasoning-блок в грамматику не
влезает, и с размышлениями модель возвращает пустой `content` (замер на ноде).

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

    # Модель крутится на нашей машине (vLLM на GX10) — одна карта на все роли.
    local = True

    def model_get(self, model_name: str, framework_model: AiFrameworkModel) -> Model:
        provider = OpenAIProvider(
            base_url=config_get(f'{self.name.upper()}_{self._size(model_name)}_API_URL'),
            api_key=config_get(self.name.upper() + '_API_KEY'),
            http_client=self.http_client,
        )

        extra_body: dict = {'chat_template_kwargs': {'enable_thinking': framework_model.thinking}}

        if framework_model.gbnf_grammar:
            # Грамматика и размышления в одном ответе не живут: thinking-блок в
            # грамматику не влезает, и с включёнными размышлениями модель возвращает
            # пустой `content` (замер на ноде). Куда идёт грамматика, там thinking
            # гасится — независимо от просьбы шага.
            extra_body['chat_template_kwargs']['enable_thinking'] = False
            extra_body['grammar'] = framework_model.gbnf_grammar

        return OpenAIChatModel(
            self.raw_model(model_name),
            provider=provider,
            settings=ModelSettings(extra_body=extra_body),
        )

    def raw_model(self, model_name: str) -> str:
        """
        Имя, которое знает сервер: `GX10_<РАЗМЕР>_MODEL`, иначе `GX10_MODEL`.

        vLLM принимает только имя, под которым поднял модель, и на реестровое
        отвечает 404; переопределение по размеру даёт той модели, что сидит на
        своём порту, своё имя, а `GX10_MODEL` — общее на машину. Ни одной
        переменной нет — остаётся имя из реестра, как было при llama.cpp.
        """
        return str(config_get(f'{self.name.upper()}_{self._size(model_name)}_MODEL')
                   or config_get(self.name.upper() + '_MODEL')
                   or model_name)

    def raw_endpoint(self, model_name: str) -> tuple[str, str]:
        """
        Адрес свой на каждый размер модели — то же правило, что и при сборке.

        Ключ один на все размеры: машина одна, разные только порты.
        """
        return (str(config_get(f'{self.name.upper()}_{self._size(model_name)}_API_URL') or ''),
                str(config_get(self.name.upper() + '_API_KEY') or ''))

    def raw_body_no_thinking(self) -> dict:
        """
        Размышления гасит аргумент шаблона чата, а не поле запроса (так же было и
        при llama.cpp, и под vLLM шаблон Qwen его понимает).

        Значение передаём явно: у шаблона своё умолчание, и «не передали» здесь
        означает не «выключено», а «как решит модель». Проверено живьём на vLLM —
        с `false` ответ непустой, с `true` на малом бюджете — пустой `content`
        при непустом `reasoning`.
        """
        return {'chat_template_kwargs': {'enable_thinking': False}}

    @staticmethod
    def _size(model_name: str) -> str:
        """Размер модели из хвоста имени: `qwen-medium` -> `MEDIUM`, он же часть адреса."""
        return model_name.split('-')[-1].upper()

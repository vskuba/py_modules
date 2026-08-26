from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel


@dataclass
class AiFrameworkModel:

    # params for framework using
    framework_class = str
    name: str
    prompt_user: str
    prompt_system: str
    user_id: int
    request_uuid: str
    session_uuid: str
    companion_id: int = 0
    tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    on_complete: Callable | None = None
    on_tokens: Callable | None = None
    is_gui_mode: bool = True
    is_transition: bool = False
    session_disabled: bool = False
    memory_short_length: int = 10
    entity_agent: dict = field(default_factory=dict)
    entities_llm: list = field(default_factory=list)
    entity_llm_current: dict = field(default_factory=dict)
    response_model: str | BaseModel = str

    node_id: int | None = None
    llm_parallel_per_agent_max: int = 1
    tokens_input: int | None = None
    tokens_output: int | None = None
    thinking_enable: bool = False

    # params for operation using
    metadata: dict = field(default_factory=dict)

    @property
    def thinking(self) -> bool:
        """
        Давать ли модели размышления на этом ходе.

        Два источника, и они про разное. Шаг workflow (`thinking_enable`) говорит,
        нужны ли размышления **этой работе**: разбор задачи от них выигрывает, а
        перекладывание готового ответа в JSON — нет. Колонка `llm.reasoning_disable`
        говорит, можно ли их вообще давать **этой модели**.

        Запрет модели сильнее разрешения шага, и иначе быть не может: запрет ставят
        там, где размышления ломают ответ, а шаг про конкретную модель ничего не
        знает — он один на все модели ноды и переживает их замену.

        Поймано на локальной модели: с включёнными размышлениями она тратит на них
        весь бюджет ответа и возвращает пустой `content` (та же причина, по которой
        `enable_thinking: false` зашит в `~/.claude/bin/gx10`).

        Значение из БД приводим сами: `TINYINT(1)` приезжает единицей, но та же
        строка приходит из seed и чужих клиентов, где ноль мог стать строкой `'0'`,
        а `bool('0')` — истина, то есть запрет там, где его не ставили.
        """
        disabled = (self.entity_llm_current or {}).get('reasoning_disable')
        if isinstance(disabled, str):
            disabled = disabled.strip().lower() in ('1', 'true', 'yes', 'on')

        return bool(self.thinking_enable) and not disabled
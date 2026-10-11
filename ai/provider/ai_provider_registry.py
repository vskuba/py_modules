"""
Выбор стратегии по имени модели и сборка модели ею.

Единственная точка, которая знает обо всех сервисах разом. Всё, что различается
между ними, лежит в их файлах; здесь только соответствие «префикс имени — класс».

Реестр перечислен явно, а не собирается обходом модуля. Список сервисов — часть
контракта: по нему видно, что вообще поддерживается, а имя незнакомого сервиса
должно давать внятный отказ, а не молчаливый пропуск.

Здесь же — **сторож режима подмены** (`ai_provider_registry_fake_guard_start`):
знать адреса всех живых сервисов может только тот, кто знает все сервисы.
"""
import contextvars

import httpx

from pydantic_ai.models import Model

from ai.framework.ai_framework_model import AiFrameworkModel
from ai.provider.ai_provider import AiProvider
from ai.provider.ai_provider_fake import AiProviderFake, ai_provider_fake_on
from ai.provider.ai_provider_gemini import AiProviderGemini
from ai.provider.ai_provider_gx10 import AiProviderGx10
from ai.provider.ai_provider_openai import (AiProviderCerebras, AiProviderClaude,
                                            AiProviderGroq, AiProviderHuggingface,
                                            AiProviderMistral, AiProviderOllama,
                                            AiProviderOpenai)
from ai.provider.ai_provider_openrouter import AiProviderOpenrouter
from config.config import config_get

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
        # ⚠ Подменный сервис тестов: сюда попадают, только когда
        # у сервера включён `LLM_FAKE`. В базе моделей его нет.
        AiProviderFake,
    )
}

# Идёт ли этот ход к живой модели **намеренно**. Ставится только
# `ai_provider_registry_live_allowed`, читается только сторожем.
#
# ⚠ `ContextVar`, а не модульный флаг: сервер обслуживает запросы вперемешку, и
# общий флаг, поднятый пингом, открыл бы дверь соседнему прогону в ту же
# миллисекунду.
_LIVE_ALLOWED = contextvars.ContextVar('ai_provider_registry_live_allowed',
                                       default=False)


class AiProviderFakeEscapeError(RuntimeError):
    """Выход к живой модели при включённой подмене.

    ⚠ Это ошибка **нашей** установки, а не сбой сервиса: подмену включают,
    чтобы живой модели не касались вовсе, и каждый такой запрос — отнятый у
    живого человека ответ.
    """


def ai_provider_registry_fake_guard_start(model_names=()):
    """Запретить выход к живым LLM, пока включена подмена. Вернуть снятие.

    Зовут из точки входа сервера (`main_uvicorn`, lifespan) — там же, где
    поднимается всё остальное. Без включённой подмены не делает ничего.

    Args:
        model_names: имена моделей установки (`gx10/qwen-small`, …) — обычно
            колонка `name` таблицы моделей. ⚠ Нужны потому, что адрес сервиса
            бывает зависит от модели: у gx10 он свой на каждый размер
            (`GX10_SMALL_API_URL`), и по имени сервиса его не угадать. Пусто —
            сторож прикроет только сервисы с общим адресом.

    ## ⚠⚠ Зачем сторож, если подмена уже перехватывает ход

    Перехват стоит в одном месте — сборке хода (`llm_model_get`). Мимо него есть
    пути: зрение (`ai_vision`), генерация картинок, пинг пула — все они берут
    адрес сервиса сами (`raw_endpoint`, `<СЕРВИС>_API_URL`). Сегодня тесты туда
    не заходят, завтра зайдут, и уйдут к живой модели **молча**: подмена не
    кричит о том, что её обошли.

    ⚠ Цена молчания измерена: выключатель подмены пролежал неподключённым с
    24.09 по 11.10.2026, все `tests_llm` ходили к живой модели, прогон сюиты шёл
    79 минут вместо шести, а понять это можно было только запросом в базу — ни
    одного обращения к `fake/echo` за всю её историю.

    ## ⚠ Почему на транспорте httpx, а не на сборке провайдера

    Тот же довод, что у `http_mock_guard`: клиентов в процессе полдюжины, у
    каждой двери свой, и сторож на сборке пропустил бы дверь, которую заведут
    завтра. Транспорт — одна дверь на все клиенты процесса.

    ⚠⚠ Запрещаются **только адреса LLM-сервисов**, а не «всё наружу»: сервер
    ходит к соседу, в Qdrant, в Telegram и в браузер, и это его работа. Список
    адресов собирается из окружения по именам сервисов реестра.
    """
    if not ai_provider_fake_on():
        return lambda: None

    forbidden = ai_provider_registry_live_hosts(model_names)

    real_async = httpx.AsyncHTTPTransport.handle_async_request
    real_sync = httpx.HTTPTransport.handle_request

    def _check(url):
        if _LIVE_ALLOWED.get():
            return

        host = str(getattr(url, 'host', '') or '')
        port = int(getattr(url, 'port', 0) or 0)

        if (host, port) not in forbidden and (host, 0) not in forbidden:
            return

        raise AiProviderFakeEscapeError(
            f'выход к живой модели при включённой подмене: {url}. '
            f'Подмена (`LLM_FAKE`) включена, значит живой модели касаться '
            f'нельзя — нода одна на тесты и на прод.\n'
            f'⚠ Ход workflow уводит на подмену `llm_model_get`; этот запрос '
            f'идёт мимо него (зрение, картинки, свой httpx) и должен либо тоже '
            f'уйти на подмену, либо объявить себя живым намеренно — '
            f'`with ai_provider_registry_live_allowed():`.')

    # ⚠ Имена обёрток свои, а не общие `_async`/`_sync`: на транспорте может
    # стоять ещё один сторож (в сюите — `http_mock_guard`), и по имени в
    # трейсбеке должно быть видно, чей отказ. По нему же свой сторож узнают
    # тесты.
    async def _ai_provider_fake_guard_async(self, request):
        _check(request.url)

        return await real_async(self, request)

    def _ai_provider_fake_guard_sync(self, request):
        _check(request.url)

        return real_sync(self, request)

    httpx.AsyncHTTPTransport.handle_async_request = _ai_provider_fake_guard_async
    httpx.HTTPTransport.handle_request = _ai_provider_fake_guard_sync

    def stop():
        httpx.AsyncHTTPTransport.handle_async_request = real_async
        httpx.HTTPTransport.handle_request = real_sync

    return stop


def ai_provider_registry_live_hosts(model_names=()) -> set:
    """Адреса живых LLM-сервисов: пары `(хост, порт)`.

    Два источника, и второй обязателен:

    1. **Общий адрес сервиса** (`<СЕРВИС>_API_URL`) — по именам реестра;
    2. **Адрес под конкретную модель** — у самого провайдера (`raw_endpoint`),
       для каждого имени из `model_names`.

    ⚠⚠ Без второго список неполон, и неполон молча. У gx10 `GX10_API_URL` пуст
    вовсе: адрес свой на каждый размер (`GX10_SMALL_API_URL`), и спросить его
    можно только с именем модели в руках. А gx10 — ровно та модель, к которой
    ходили тесты, пока подмена была не подключена. Сторож без неё прикрывал бы
    всё, кроме главного.

    ⚠ Угадывать по шаблону имён (`<СЕРВИС>_*_API_URL`) нельзя: под него
    подпадает `CLAUDE_SHELL_API_URL`, который к моделям отношения не имеет, и
    сторож зарубил бы оболочку Claude Code.

    ⚠ Подмена из списка исключена: к ней-то и ведут все дороги при включённом
    выключателе.

    ⚠ Порт `0` значит «любой у этого хоста».
    """
    hosts = set()

    def _add(raw: str) -> None:
        raw = str(raw or '').strip()
        if not raw:
            return

        url = httpx.URL(raw if '://' in raw else f'http://{raw}')
        if url.host:
            hosts.add((str(url.host), int(url.port or 0)))

    for name, provider_class in AI_PROVIDER_REGISTRY.items():
        if provider_class is AiProviderFake:
            continue

        _add(config_get(name.upper() + '_API_URL'))

    for model_name in model_names or ():
        provider_name, _, model_name_clean = str(model_name or '').partition('/')
        provider_class = AI_PROVIDER_REGISTRY.get(provider_name.lower())

        if not provider_class or provider_class is AiProviderFake or not model_name_clean:
            continue

        _add(provider_class(None).raw_endpoint(model_name_clean)[0])

    return hosts


class ai_provider_registry_live_allowed:  # noqa: N801 — контекстный менеджер
    """Объявить, что этот участок кода идёт к живой модели **намеренно**.

    Нужен ровно одному потребителю — пингу пула моделей (`team_llm_ping`): его
    предмет в том и состоит, чтобы узнать, жив ли **настоящий** сервис. Уведи
    его на подмену, и он начнёт отвечать «все живы» всегда, то есть врать
    именно в том, ради чего заведён.

    ⚠ Больше его звать неоткуда. Появился второй повод — сперва спроси, нельзя
    ли вместо этого увести вызов на подмену: разрешение здесь значит, что на
    тестовой машине запрос уйдёт к модели, отвечающей живым людям.
    """

    def __enter__(self):
        self._token = _LIVE_ALLOWED.set(True)

        return self

    def __exit__(self, *_):
        _LIVE_ALLOWED.reset(self._token)

        return False


def ai_provider_registry_get(model_name: str,
                             http_client: httpx.AsyncClient = None) -> AiProvider | None:
    """
    Стратегия сервиса по имени модели — без сборки самой модели.

    Нужна тем, кто говорит с сервисом сырым HTTP и модель не собирает: пингу пула
    (`team_llm_ping`) от стратегии нужны только адрес с ключом и поля, глушащие
    размышления. Собирать ради этого объект модели значило бы требовать `thinking`,
    настройки хода и прочее, которого у пинга нет и быть не может.

    `None` — сервис незнакомый. Здесь это не отказ, в отличие от сборки модели:
    вызов сырого запроса вправе решить, что делать с неизвестным именем, — пинг,
    например, помечает такую модель «проверить не смогли», а не «мертва».

    `http_client` необязателен: сырому вызову он не нужен, у него свой.
    """
    provider_name, _, model_name_clean = str(model_name or '').partition('/')
    provider_class = AI_PROVIDER_REGISTRY.get(provider_name.lower())

    if not provider_class or not model_name_clean:
        return None

    return provider_class(http_client)


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

    ⚠ Подмену (`LLM_FAKE`) здесь **не спрашивают**: соответствие «имя → класс» —
    это весь предмет реестра, и подмена сделала бы его ответ зависящим от
    окружения процесса. Решает тот, кто собирает ход, — `llm_model_get`
    (`ai/framework/ai_framework.py`).
    """
    provider_name, _, model_name_clean = str(model_name or '').partition('/')
    provider_class = AI_PROVIDER_REGISTRY.get(provider_name.lower())

    if not provider_class or not model_name_clean:
        raise ValueError(
            f"Для LLM модели '{model_name}' не найден подходящий провайдер. "
            f"Известные: {', '.join(sorted(AI_PROVIDER_REGISTRY))}. Проверьте конфигурацию.")

    return provider_class(http_client).model_get(model_name_clean, framework_model)

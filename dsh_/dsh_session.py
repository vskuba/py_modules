"""Сессии хоста DeepSeek Harness: список, промпт, отмена, архив, журнал — обычным HTTP.

Карточка сессии — то же, что вкладка в браузере хоста: свой журнал событий, свой агент
(`agent.id == session.id`), своя модель. Всё, что делает этот файл, человек видит в
интерфейсе на том же порту вживую.

Журнал читается **без вебсокета**. Хост отдаёт хвост журнала первым кадром потока
`session/follow`, но тот же хвост даёт и обычный вызов `session/page` — ему нужна лишь
засечка журнала (`throughSeq`), не дальше текущей. Её приносит `session/list`: у каждой
карточки там `projections.asOfSeq`. Засечка проекций может отставать от журнала на
запись-другую — тогда самое свежее событие придёт следующим опросом, а не потеряется.

Конец круга узнаётся **по журналу**, а не опросом «идёт ли круг» (`running` в списке):
короткий круг (3–5 с) опрос пропускает, если хост в эти секунды занят — замечено, что
после первого круга новой карточки `/api` молчит до 30 с. Запись `turn/end` в журнале
остаётся навсегда, её не проспать.
"""
import time
import uuid

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DshApiError, DshApiUnreachable, dsh_api_call

# Сколько записей журнала брать на одну видимую реплику: между промптом и ответом лежат
# шаги инструментов и служебные вставки хоста, а окно хост режет по сообщениям.
DSH_SESSION_SPAN = 8

# Окно журнала при ожидании ответа: свой промпт должен в него попадать и в долгом
# круге с десятками шагов инструментов.
DSH_SESSION_REPLY_SPAN = 40

# Шаг опроса журнала в ожидании конца круга.
DSH_SESSION_POLL = 2.0


def dsh_session_list(*, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Все сохранённые карточки хоста, свежие сверху.

    Returns:
        Список `SessionSummary`: `sessionId`, `updatedAt`, `running`, `blank`,
        `agentAvailable`, иногда `cwd` и `projections` (там `values.title` и `asOfSeq`).
        ⚠ Архивные карточки в списке **остаются** — архив виден только реестром проектов.
    """
    return (dsh_api_call('session/list', {'_request': {}}, base=base) or {}).get('items', [])


def dsh_session_find(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Карточка из списка по id; пустой словарь — такой нет."""
    for item in dsh_session_list(base=base):
        if item.get('sessionId') == session:
            return item
    return {}


def dsh_session_title(item: dict) -> str:
    """Подпись карточки, как её видит человек; пустая — подписи ещё нет."""
    return (((item.get('projections') or {}).get('values') or {}).get('title')) or ''


def dsh_session_prompt(session: str, text: str, mode: str = 'queue', *,
                       base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Отправить текст в карточку — как строку, набранную человеком.

    Args:
        mode: `queue` — встать в очередь карточки (несколько вызовов подряд — очередь
            промптов, агент разберёт их по одному); `steer` — вклиниться в идущий круг.

    Returns:
        `{"accepted": true}` — расписка о приёме, не ответ.

    Raises:
        DshApiError: `session/not-found`, `session/agent-busy`, `session/model-unavailable`.
    """
    request = {'requestId': str(uuid.uuid4()), 'sessionId': session, 'mode': mode,
               'content': [{'type': 'text', 'text': text}]}
    return dsh_api_call('session/prompt', {'request': request}, base=base) or {}


def dsh_session_cancel(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Прервать идущий круг карточки."""
    return dsh_api_call('session/cancel', {'request': {'sessionId': session}}, base=base) or {}


def dsh_session_archive(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Убрать карточку в архив (идемпотентно). Returns: все архивные id хоста."""
    value = dsh_api_call('workspace/archiveSession', {'request': {'sessionId': session}},
                         base=base) or {}
    return value.get('archivedSessionIds', [])


def dsh_session_history(session: str, messages: int = DSH_SESSION_SPAN, *,
                        base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Хвост журнала карточки: последние `messages` сообщений со всеми их событиями.

    Returns:
        События `{"type", "seq", "time", "data"}` по возрастанию `seq`; пусто — журнала
        нет или карточка без проекций (пустая, ни одного круга).
    """
    cursor = dsh_session_cursor(session, base=base)
    if cursor == 0:
        return []
    request = {'address': {'kind': 'session', 'sessionId': session},
               'maxMessages': messages, 'throughSeq': cursor, 'beforeSeq': cursor + 1}
    page = dsh_api_call('session/page', {'request': request}, base=base) or {}
    return [entry.get('event', entry) for entry in page.get('records') or []
            if isinstance(entry, dict)]


def dsh_session_cursor(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> int:
    """Засечка журнала карточки (`projections.asOfSeq`); 0 — журнала ещё нет.

    Raises:
        DshApiError: `session/not-found` — такой карточки нет: опечатка в id не должна
            оборачиваться получасом ожидания пустого журнала.
    """
    item = dsh_session_find(session, base=base)
    if not item:
        raise DshApiError(f'session/not-found: {session}')
    return int((item.get('projections') or {}).get('asOfSeq') or 0)


def dsh_session_dialog(session: str, count: int = 10, *,
                       base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Последние `count` реплик карточки — промпты и ответы, без шагов инструментов.

    Промпт человека (и наш) — `user/message` с `source.kind == "user"`; служебные вставки
    хоста (инструкции проекта, снимок песочницы) идут тем же типом, но своим `kind`.

    Returns:
        `[{"role": "user"|"assistant", "time", "seq", "text"}]` по возрастанию.
    """
    if count <= 0:
        return []
    rows = []
    for event in dsh_session_history(session, count * DSH_SESSION_SPAN, base=base):
        row = _dialog_row(event)
        if row is not None:
            rows.append(row)
    return rows[-count:]


def dsh_session_answer_last(session: str, after: int = 0, *,
                            base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Текст последнего ответа агента новее засечки `after`; пусто — ответа нет."""
    for event in reversed(dsh_session_history(session, base=base)):
        if event.get('seq', 0) <= after:
            break
        row = _dialog_row(event)
        if row is not None and row['role'] == 'assistant':
            return row['text']
    return ''


def dsh_session_reply_wait(session: str, after: int, text: str, timeout: float = 1800, *,
                           base: str = DSH_API_ADDR_DEFAULT):
    """Дождаться ответа карточки на **свой** промпт и вернуть его текст.

    Ответ привязан к своему промпту, а не к первому концу круга после засечки: карточка
    могла быть занята чужим кругом (работа руками, заход доски, соседняя задача того же
    `fanout`), и первым после засечки лёг бы его `turn/end` — чужой ответ выдался бы за
    наш, а наш потерялся. Поэтому: в журнале ищется свой `user/message` — новее засечки
    `after` и с тем же текстом; затем `turn/end` после него; ответ — последнее сообщение
    агента между ними, а не текущий хвост (следующий круг мог уже кончиться).

    Переспрашивается только «хост не ответил» (`DshApiUnreachable`); прочие ошибки —
    нет такой карточки, 401, отказ метода — уходят наверх сразу, ожиданием их не исправить.

    ⚠ Два одинаковых промпта в одну карточку подряд неразличимы по тексту: оба вызова
    найдут первый. Разные задачи в одну карточку — различимы.

    Args:
        after: засечка журнала до отправки (`dsh_session_cursor`).
        text: текст отправленного промпта.
        timeout: потолок ожидания, с.

    Returns:
        Текст ответа (пустая строка — круг кончился без текста), либо `None` — не
        дождались за `timeout`.
    """
    deadline = time.time() + timeout
    asked = ' '.join(str(text).split())
    mine = 0
    while True:
        try:
            events = dsh_session_history(session, DSH_SESSION_REPLY_SPAN, base=base)
        except DshApiUnreachable:
            events = []                      # хост занят — спросим следующим витком
        if mine == 0:
            mine = next((event.get('seq', 0) for event in events
                         if event.get('seq', 0) > after and _is_prompt(event, asked)), 0)
        if mine:
            end = next((event.get('seq', 0) for event in events
                        if event.get('type') == 'turn/end' and event.get('seq', 0) > mine), 0)
            if end:
                return _reply_between(events, mine, end)
        if time.time() >= deadline:
            return None
        time.sleep(DSH_SESSION_POLL)


def dsh_session_message_text(message: dict) -> str:
    """Видимый текст сообщения: только блоки `text`, без рассуждений и инструментов."""
    parts = [block.get('text', '') for block in message.get('content', [])
             if isinstance(block, dict) and block.get('type') == 'text']
    return ''.join(parts).strip()


# ── детали реализации ──

def _dialog_row(event: dict):
    """Событие журнала как реплика диалога, или None — это не реплика."""
    kind = event.get('type')
    data = event.get('data') or {}
    if kind == 'user/message':
        if (data.get('source') or {}).get('kind') != 'user':
            return None
        text = dsh_session_message_text(data)
        role = 'user'
    elif kind == 'assistant/message':
        text = dsh_session_message_text(data.get('message') or {})
        role = 'assistant'
    else:
        return None
    if not text:
        return None
    return {'role': role, 'time': event.get('time'), 'seq': event.get('seq'), 'text': text}


def _is_prompt(event: dict, asked: str) -> bool:
    """Это наш промпт: реплика человека (`source.kind == "user"`) с тем же текстом."""
    row = _dialog_row(event)
    return row is not None and row['role'] == 'user' and ' '.join(row['text'].split()) == asked


def _reply_between(events: list, start: int, end: int) -> str:
    """Последний ответ агента строго между своим промптом и концом его круга."""
    text = ''
    for event in events:
        if start < event.get('seq', 0) < end:
            row = _dialog_row(event)
            if row is not None and row['role'] == 'assistant':
                text = row['text']
    return text

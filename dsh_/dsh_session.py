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
import base64
import mimetypes
import os
import time
import uuid

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DshApiError, DshApiUnavailable, DshApiUnreachable, \
    dsh_api_call

# Сколько записей журнала брать на одну видимую реплику: между промптом и ответом лежат
# шаги инструментов и служебные вставки хоста, а окно хост режет по сообщениям.
DSH_SESSION_SPAN = 8

# Картинки, которые хост принимает частью промпта.
DSH_SESSION_IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')

# Окно журнала при ожидании ответа: свой промпт должен в него попадать и в долгом
# круге с десятками шагов инструментов.
DSH_SESSION_REPLY_SPAN = 40

# Шаг опроса журнала в ожидании конца круга.
DSH_SESSION_POLL = 2.0
# Страница журнала долгой карточки (сотни шагов с выводами инструментов) тяжела: под
# нагрузкой хост отдаёт её дольше общих 10 с, и verify/history падали с «timed out».
DSH_SESSION_PAGE_TIMEOUT = 90.0


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


def dsh_session_prompt(session: str, text: str, mode: str = 'queue', images: list = None, *,
                       base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Отправить текст в карточку — как строку, набранную человеком.

    Args:
        mode: `queue` — встать в очередь карточки (несколько вызовов подряд — очередь
            промптов, агент разберёт их по одному); `steer` — вклиниться в идущий круг.
        images: пути к картинкам (png, jpeg, webp, gif) — едут частями промпта рядом с
            текстом, как картинка, приложенная человеком в поле ввода.

    Returns:
        `{"accepted": true}` — расписка о приёме, не ответ.

    Raises:
        DshApiError: `session/not-found`, `session/agent-busy`, `session/model-unavailable`.
    """
    content = [{'type': 'text', 'text': text}] + [_image_part(path) for path in images or []]
    request = {'requestId': str(uuid.uuid4()), 'sessionId': session, 'mode': mode,
               'content': content}
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
    page = dsh_api_call('session/page', {'request': request}, base=base,
                        timeout=DSH_SESSION_PAGE_TIMEOUT) or {}
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


def dsh_session_turn_reply(session: str, after: int, text: str, timeout: float = 1800, *,
                           base: str = DSH_API_ADDR_DEFAULT):
    """Дождаться конца круга на **свой** промпт: текст ответа и почему круг кончился.

    Ответ привязан к своему промпту, а не к первому концу круга после засечки: карточка
    могла быть занята чужим кругом (работа руками, заход доски, соседняя задача того же
    `fanout`), и первым после засечки лёг бы его `turn/end`. Поэтому: в журнале ищется
    свой `user/message` — новее засечки `after` и с тем же текстом; затем `turn/end`
    после него; ответ — последнее сообщение агента между ними, а не текущий хвост.

    ⚠ Перезапуск хоста закрывает идущий круг записью `turn/end` с причиной
    `interrupted` — промежуточная реплика («ищу…») тогда не ответ. Причину отдаём наверх:
    решать, продолжать ли круг, — вызывающему (`dsh_board_ask` продолжает сам).

    Переспрашиваются «хост не ответил» и «служба ещё не поднялась»; прочие ошибки —
    нет такой карточки, 401, отказ метода — уходят наверх сразу.

    ⚠ Два одинаковых промпта в одну карточку подряд неразличимы по тексту.

    Returns:
        `{"text", "reason", "seq"}` — `reason` из `turn/end` (`completed`, `interrupted`,
        …), `seq` — его номер в журнале; `None` — не дождались за `timeout`.
    """
    deadline = time.time() + timeout
    asked = ' '.join(str(text).split())
    mine = 0
    while True:
        try:
            events = dsh_session_history(session, DSH_SESSION_REPLY_SPAN, base=base)
        except (DshApiUnreachable, DshApiUnavailable):
            events = []                      # хост занят или поднимается — спросим следующим витком
        if mine == 0:
            mine = next((event.get('seq', 0) for event in events
                         if event.get('seq', 0) > after and _is_prompt(event, asked)), 0)
        if mine:
            end = next((event for event in events
                        if event.get('type') == 'turn/end' and event.get('seq', 0) > mine), None)
            if end is not None:
                reason = (((end.get('data') or {}).get('reason')) or {}).get('kind') or 'completed'
                return {'text': _reply_between(events, mine, end.get('seq', 0)), 'reason': reason,
                        'seq': end.get('seq', 0)}
        if time.time() >= deadline:
            return None
        time.sleep(DSH_SESSION_POLL)


def dsh_session_turn_start_wait(session: str, after: int, timeout: float = 60, *,
                                base: str = DSH_API_ADDR_DEFAULT) -> bool:
    """Дождаться в журнале начала круга (`turn/start`) новее засечки `after`.

    Returns:
        True — круг начался; False — не начался за `timeout` (промпт ждёт в очереди
        карточки или хост занят).
    """
    deadline = time.time() + timeout
    while True:
        try:
            events = dsh_session_history(session, base=base)
        except (DshApiUnreachable, DshApiUnavailable):
            events = []
        if any(e.get('type') == 'turn/start' and e.get('seq', 0) > after for e in events):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(DSH_SESSION_POLL)


def dsh_session_reply_wait(session: str, after: int, text: str, timeout: float = 1800, *,
                           base: str = DSH_API_ADDR_DEFAULT):
    """Текст ответа на свой промпт (`dsh_session_turn_reply`); `None` — не дождались.

    Причину конца круга не отдаёт — кому важен оборванный круг, тот зовёт
    `dsh_session_turn_reply`.
    """
    reply = dsh_session_turn_reply(session, after, text, timeout, base=base)
    return None if reply is None else reply['text']


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


def _image_part(path: str) -> dict:
    """Картинка с диска — часть промпта `{type: image, mediaType, data, name}`."""
    media = mimetypes.guess_type(path)[0] or ''
    if media not in DSH_SESSION_IMAGE_TYPES:
        raise ValueError(f'{path}: картинка должна быть {", ".join(DSH_SESSION_IMAGE_TYPES)}, а не {media or "?"}')
    with open(os.path.expanduser(path), 'rb') as handle:
        data = base64.b64encode(handle.read()).decode('ascii')
    return {'type': 'image', 'mediaType': media, 'data': data, 'name': os.path.basename(path)}

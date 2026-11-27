"""Сессии хоста DeepSeek Harness: список, промпт, отмена, архив, журнал — обычным HTTP.

Карточка сессии — то же, что вкладка в браузере хоста: свой журнал событий, свой агент
(`agent.id == session.id`), своя модель. Всё, что делает этот файл, человек видит в
интерфейсе на том же порту вживую.

Журнал читается **без вебсокета**. Хост отдаёт хвост журнала первым кадром потока
`session/follow`, но тот же хвост даёт и обычный вызов `session/page` — ему нужна лишь
засечка журнала (`throughSeq`), не дальше текущей. Засечку одной карточки называет сам
`session/page`: запрос «дальше конца» он отклоняет текстом `past cursor N` — 0,2 с.
⚠⚠ Не `session/list` ради засечки: он несёт проекции **всех** карточек (346 карточек —
2,5 МБ и 43 с), и ожидание ответа, звавшее его каждые 2 с, держало хост в непрерывной
сборке мусора на 6 ГБ кучи. Сам список кэшируется на диске, общим для всех процессов
(`DSH_SESSION_LIST_TTL`), и перечитывает его один процесс за раз.

Конец круга узнаётся **по журналу**, а не опросом «идёт ли круг» (`running` в списке):
короткий круг (3–5 с) опрос пропускает, если хост в эти секунды занят — замечено, что
после первого круга новой карточки `/api` молчит до 30 с. Запись `turn/end` в журнале
остаётся навсегда, её не проспать.
"""
import base64
import fcntl
import json
import mimetypes
import os
import re
import time
import uuid

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT, DshApiError, DshApiUnavailable, DshApiUnreachable, \
    dsh_api_call

# Сколько записей журнала брать на одну видимую реплику: между промптом и ответом лежат
# шаги инструментов и служебные вставки хоста, а окно хост режет по сообщениям.
DSH_SESSION_SPAN = 8

# Картинки, которые хост принимает частью промпта.
DSH_SESSION_IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')

# Окно журнала при ожидании ответа: свой промпт должен в него попадать и в долгом
# круге с десятками шагов инструментов.
DSH_SESSION_REPLY_SPAN = 40

# Ходьба назад по журналу (`dsh_session_round_pair`, `dsh_session_turn_log`): окно страниц
# и их число. 6 × 40 сообщений покрывают круг в ~600 записей — больше в живых карточках
# не было (замер 24–27.11), а каждая лишняя страница — тяжёлый `session/page`.
DSH_SESSION_PAIR_MESSAGES = 40
DSH_SESSION_PAIR_PAGES = 6

# Шаг опроса журнала в ожидании конца круга.
DSH_SESSION_POLL = 2.0
# Страница журнала долгой карточки и список карточек (сотни шагов с выводами инструментов) тяжела: под
# нагрузкой хост отдаёт её дольше общих 10 с, и verify/history падали с «timed out».
DSH_SESSION_PAGE_TIMEOUT = 90.0
# Список карточек — общий кэш на диске (каталог хоста): моложе TTL — с диска, старше —
# перечитывает один процесс под замком, остальные тем временем берут прошлый снимок.
DSH_SESSION_LIST_CACHE = 'dsh-session-list.json'
DSH_SESSION_LIST_TTL = 60.0
# Засечка «дальше конца» — её хост отклоняет, называя настоящую.
DSH_SESSION_PROBE_SEQ = 10 ** 12


def dsh_session_list(fresh: bool = False, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Все сохранённые карточки хоста, свежие сверху — из общего кэша, если он свежий.

    Args:
        fresh: перечитать у хоста, даже если кэш моложе `DSH_SESSION_LIST_TTL`.

    Returns:
        Список `SessionSummary`: `sessionId`, `updatedAt`, `running`, `blank`,
        `agentAvailable`, иногда `cwd` и `projections` (там `values.title` и `asOfSeq`).
        ⚠ Архивные карточки в списке **остаются** — архив виден только реестром проектов.
    """
    path = _cache_path(base)
    cached = _cache_read(path)
    if cached is not None and not fresh and time.time() - cached['at'] < DSH_SESSION_LIST_TTL:
        return cached['items']
    with open(path + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if cached is not None else 0))
        except BlockingIOError:
            return cached['items']           # перечитывает сосед — берём прошлый снимок
        again = _cache_read(path)            # пока ждали замок, сосед мог уже перечитать
        if again is not None and (again['at'] > (cached or {}).get('at', 0) or
                                  (not fresh and time.time() - again['at'] < DSH_SESSION_LIST_TTL)):
            return again['items']
        items = (dsh_api_call('session/list', {'_request': {}}, base=base,
                              timeout=DSH_SESSION_PAGE_TIMEOUT) or {}).get('items', [])
        _cache_write(path, {'at': time.time(), 'items': items})
        return items


def dsh_session_find(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Карточка из списка по id; пустой словарь — такой нет.

    Нет в кэше — список перечитывается один раз: карточку могли завести после снимка.
    """
    for fresh in (False, True):
        for item in dsh_session_list(fresh, base=base):
            if item.get('sessionId') == session:
                return item
    return {}


def dsh_session_title(item: dict) -> str:
    """Подпись карточки, как её видит человек; пустая — подписи ещё нет."""
    return (((item.get('projections') or {}).get('values') or {}).get('title')) or ''


def dsh_session_asking(sessions: list, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Какие из карточек стоят и ждут ответа человека: `{session: текст вопроса}`.

    Субагент спросил (`ask_user_question`) — его работа на паузе, пока не ответят. Признак —
    в хвосте журнала карточки (`session/page`, доли секунды): `tool/call` вопроса, на который
    ещё нет `tool/result` с тем же `callId` (`data.message.source.callId`).

    Args:
        sessions: какие карточки смотреть — обычно работающие (их не больше числа моторов).

    ⚠ Не по проекции `userQuestions.active` из списка карточек: список — общий кэш до 60 с
    (`session/list` — самый дорогой вызов хоста), и живой вопрос в нём не появлялся —
    замер на карточке, ждущей ответа.
    """
    asking = {}
    for session in sessions:
        try:
            events = dsh_session_history(session, 2, base=base)
        except DshApiError:
            continue
        pending = {}
        for event in events:
            data = event.get('data') or {}
            if event.get('type') == 'tool/call' and data.get('name') == 'ask_user_question':
                pending[data.get('callId')] = _question_text(data.get('arguments'))
            elif event.get('type') == 'tool/result':
                pending.pop((((data.get('message') or {}).get('source')) or {}).get('callId'), None)
        if pending:
            asking[session] = list(pending.values())[-1]
    return asking


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
    content = [{'type': 'text', 'text': text}] + [dsh_session_image_part(path) for path in images or []]
    request = {'requestId': str(uuid.uuid4()), 'sessionId': session, 'mode': mode,
               'content': content}
    return dsh_api_call('session/prompt', {'request': request}, base=base) or {}


def dsh_session_image_part(path: str) -> dict:
    """Картинка с диска — частью промпта: `{type: 'image', mediaType, data, name}`.

    Зовут там, где промпт собирают руками: так же картинку кладёт и сам
    `dsh_session_prompt`. Цель доски картинку хранит в той же форме, но без `type`
    — круг соберёт из неё часть промпта сам.

    Raises:
        ValueError: файл — не картинка (png, jpeg, webp, gif).
    """
    media = mimetypes.guess_type(path)[0] or ''
    if media not in DSH_SESSION_IMAGE_TYPES:
        raise ValueError(f'{path}: картинка должна быть {", ".join(DSH_SESSION_IMAGE_TYPES)}, а не {media or "?"}')
    with open(os.path.expanduser(path), 'rb') as handle:
        data = base64.b64encode(handle.read()).decode('ascii')
    return {'type': 'image', 'mediaType': media, 'data': data, 'name': os.path.basename(path)}


def dsh_session_cancel(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Прервать идущий круг карточки."""
    return dsh_api_call('session/cancel', {'request': {'sessionId': session}}, base=base) or {}


def dsh_session_archive(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Убрать карточку в архив (идемпотентно). Returns: все архивные id хоста."""
    value = dsh_api_call('workspace/archiveSession', {'request': {'sessionId': session}},
                         base=base) or {}
    return value.get('archivedSessionIds', [])


def dsh_session_history(session: str, messages: int = DSH_SESSION_SPAN, cursor: int = None, *,
                        base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Хвост журнала карточки: последние `messages` сообщений со всеми их событиями.

    Returns:
        События `{"type", "seq", "time", "data"}` по возрастанию `seq`; пусто — журнала
        нет или карточка без проекций (пустая, ни одного круга).
    """
    if cursor is None:
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
    request = {'address': {'kind': 'session', 'sessionId': session}, 'maxMessages': 1,
               'throughSeq': DSH_SESSION_PROBE_SEQ, 'beforeSeq': DSH_SESSION_PROBE_SEQ + 1}
    try:
        dsh_api_call('session/page', {'request': request}, base=base)
    except (DshApiUnreachable, DshApiUnavailable):
        raise
    except DshApiError as error:
        found = re.search(r'past cursor (\d+)', str(error))
        if found:
            return int(found.group(1))
    # Хост ответил иначе (журнала нет, формат сменился) — засечка из списка.
    item = dsh_session_find(session, base=base)
    if not item:
        raise DshApiError(f'session/not-found: {session}')
    return int((item.get('projections') or {}).get('asOfSeq') or 0)


def dsh_session_dialog(session: str, count: int = 10, *,
                       base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Последние `count` реплик карточки — промпты и ответы, без шагов инструментов.

    Промпт человека (и наш) — `user/message` с `source.kind == "user"`; служебные вставки
    хоста (инструкции проекта, снимок песочницы) идут тем же типом, но своим `kind`.

    ⚠ Окно берётся по репликам, а не по записям журнала: круг с десятками шагов
    инструментов раздувает журнал в десятки раз, и одна страница до промпта не доходит —
    `dsh-board history` long-карточки показывал одни ответы, без задающих их промптов.
    Поэтому окно идёт назад страницами, пока реплик не наберётся.

    Returns:
        `[{"role": "user"|"assistant", "time", "seq", "text"}]` по возрастанию.
    """
    if count <= 0:
        return []
    rows: list = []

    def enough(events: list) -> bool:
        rows[:] = [row for event in events if (row := _dialog_row(event)) is not None]
        return len(rows) >= count

    _walk_back(session, enough, DSH_SESSION_PAIR_PAGES,
               max(count * DSH_SESSION_SPAN, DSH_SESSION_PAIR_MESSAGES), base)
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


def dsh_session_round_pair(session: str, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Пара «задача — ответ» последнего завершённого круга — по всему журналу карточки.

    `dsh_session_dialog` берёт последние `count × DSH_SESSION_SPAN` записей журнала, а в
    круге с десятками шагов инструментов до промпта так не дойти: у карточки на 616 записей
    задача лежит на 15-й, и `dsh_board_verify` объявлял её «без пары» — замеры 24–27.11
    дают 5 таких падений, каждый раз проверку делали руками заново. Здесь окно идёт от
    конца назад страницами, пока не соберутся ответ и промпт к нему.

    ⚠ Задачу мог положить не человек, а доска (`continuous_add`): в журнале это
    `agent/inbox/spliced` с `target: "next-turn"`, репликой человека оно не числится — но
    это и есть задача того круга, и как задача пары оно годится.

    Returns:
        `{'task', 'answer', 'via'}` — `via`: `user` — промпт из реплики человека,
        `goal` — клад доски, `` — пара не собрана (карточка без круга, журнал пуст).
    """
    events = _walk_back(session, _pair_enough, DSH_SESSION_PAIR_PAGES,
                        DSH_SESSION_PAIR_MESSAGES, base)
    answer, answer_seq = '', -1
    for event in events:                              # последний ответ агента — это ответ пары
        row = _dialog_row(event)
        if row is not None and row['role'] == 'assistant':
            answer, answer_seq = row['text'], event.get('seq', 0)
    task, via = '', ''
    for event in events:                              # промпт, которым начат именно этот ответ
        row = _dialog_row(event)
        if row is not None and row['role'] == 'user' and event.get('seq', 0) < answer_seq:
            task, via = row['text'], 'user'
    if not task and answer:
        # Реплики человека в окне нет: задача — последний клад доски перед ответом.
        for event in events:
            text = _spliced_goal_text(event)
            if text and event.get('seq', 0) < answer_seq:
                task, via = text, 'goal'
    return {'task': task, 'answer': answer, 'via': via}


def dsh_session_turn_log(session: str, rounds: int = 3, *,
                         base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Границы последних кругов карточки — `{turn, start, end}` по возрастанию.

    Нужны, чтобы сказать, сколько круг уже идёт и сколько он длится обычно: по `running`
    в списке карточек этого не видно, а потолок ожидания (`DSH_BOARD_WAIT_TIMEOUT`) без
    этих чисел ставят наугад и промахиваются (замер 24–27.11: 30 таймаутов ожидания,
    медиана готового круга — 36 мин при потолке 30 мин).

    Args:
        rounds: сколько завершённых кругов собрать; идущий сейчас добавляется всегда.

    Returns:
        `[{'turn', 'start', 'end'}]` — времена в секундах (`time.time`), `end` пуст при
        идущем круге; пусто — журнал карточки не читается или кругов не было.
    """
    events = _walk_back(session, lambda rows: _turn_rows(rows)[1] >= rounds,
                        DSH_SESSION_PAIR_PAGES, DSH_SESSION_PAIR_MESSAGES, base)
    return _turn_rows(events)[0]


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
    seen = -1
    while True:
        # Журнал читается, только когда засечка сдвинулась: пока агент думает, виток
        # стоит одну лёгкую пробу, а не страницу из 40 сообщений.
        try:
            cursor = dsh_session_cursor(session, base=base)
            events = dsh_session_history(session, DSH_SESSION_REPLY_SPAN, cursor, base=base) \
                if cursor != seen else []
            seen = cursor
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

def _question_text(entry) -> str:
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)          # аргументы вызова приходят строкой JSON
        except ValueError:
            return entry
    if not isinstance(entry, dict):
        return 'вопрос'
    for key in ('question', 'title', 'prompt', 'text', 'header'):
        if isinstance(entry.get(key), str) and entry[key]:
            return entry[key]
    for key in ('questions', 'request', 'items'):
        nested = entry.get(key)
        if isinstance(nested, list) and nested:
            return _question_text(nested[0])
        if isinstance(nested, dict):
            return _question_text(nested)
    return 'вопрос'


def _cache_path(base: str) -> str:
    home = os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT)
    tag = re.sub(r'[^0-9a-zA-Z]+', '-', base).strip('-')
    return os.path.join(home, f'{tag}.{DSH_SESSION_LIST_CACHE}')


def _cache_read(path: str):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value.get('items'), list) else None
    except (OSError, ValueError, AttributeError):
        return None


def _cache_write(path: str, value: dict) -> None:
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(value, handle)             # ASCII: в журналах хоста бывают одиночные суррогаты
    os.replace(tmp, path)


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


def _walk_back(session: str, enough, pages: int, messages: int, base: str) -> list:
    """События журнала от конца назад, пока `enough(events)` не скажет «хватит».

    Одна страница — `messages` сообщений; засечку следующей, более ранней страницы берём
    на единицу младше самого раннего события уже прочитанного окна. Страниц меньше
    заявленного — журнал кончился: дальше идти некуда.

    ⚠ Страница тяжёлой карточки (сотни шагов с выводами инструментов) велика и медленна
    (`DSH_SESSION_PAGE_TIMEOUT`) — потому страниц всего `DSH_SESSION_PAIR_PAGES`, а не
    «весь журнал»: обычно пара находится в первых двух окнах.
    """
    events, cursor = [], None
    for _ in range(pages):
        rows = dsh_session_history(session, messages, cursor, base=base)
        if not rows:
            break
        events = rows + events
        if enough(events):
            break
        oldest = int(rows[0].get('seq', 0) or 0)
        if cursor is not None and oldest >= cursor:
            break                                    # страница не сдвинулась назад — идти некуда
        cursor = oldest - 1
    return events


def _pair_enough(events: list) -> bool:
    """Для пары нужны ответ агента **и** промпт раньше него: одного ответа мало.

    Окно, в котором есть только ответ, — это круг с сотнями шагов инструментов: промпт
    остался страницей раньше, и ходить надо дальше, а не объявлять пару собранной.
    """
    marks = [(event.get('seq', 0), (_dialog_row(event) or {}).get('role')) for event in events]
    marks = [(seq, role) for seq, role in marks if role]
    answer = max((seq for seq, role in marks if role == 'assistant'), default=-1)
    return answer > 0 and any(seq < answer for seq, role in marks if role == 'user')


def _spliced_goal_text(event: dict) -> str:
    """Задача, положенная карточке доской (`continuous_add`), — текст вставки `next-turn`.

    Вставки `next-step` — это служебные уведомления (финиш фоновой задачи, снимок
    инструкций), а не задача; их не берём.
    """
    if event.get('type') != 'agent/inbox/spliced':
        return ''
    data = event.get('data') or {}
    if data.get('target') != 'next-turn':
        return ''
    for part in data.get('inserted') or []:
        text = dsh_session_message_text(part if isinstance(part, dict) else {})
        if text:
            return text
    return ''


def _turn_rows(events: list) -> tuple:
    """Границы кругов из событий журнала: `([{turn, start, end}], сколько завершённых)`."""
    opened = {}
    rows = []
    for event in events:
        kind = event.get('type')
        if kind not in ('turn/start', 'turn/end'):
            continue
        data = event.get('data') or {}
        turn = data.get('turn')
        clock = _event_clock(event)
        if kind == 'turn/start':
            opened[turn] = clock
        else:
            start = opened.pop(turn, None)
            if start:
                rows.append({'turn': turn, 'start': start, 'end': clock})
    rows += [{'turn': turn, 'start': clock, 'end': None} for turn, clock in opened.items()]
    rows.sort(key=lambda row: row['start'] or 0)
    return rows, sum(1 for row in rows if row['end'])


def _event_clock(event: dict) -> float:
    """Время события журнала в секундах эпохи: хост пишет его в миллисекундах."""
    value = event.get('time')
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000 if value > 1e11 else value

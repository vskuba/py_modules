"""Ответ субагента не уходит в никуда: оркестратор ждёт его фоном и просыпается по нему.

Оркестратор (Claude Code) будят только **его** фоновые процессы: кончился процесс — пришло
уведомление. Задача, отданная субагенту без ожидания (`new --ask` без `--wait`, оборванный
`fanout`, круг, продолжённый `revive`), такого процесса не имеет — субагент заканчивает,
а оркестратор, закрывший ход, об этом не узнаёт.

Поэтому каждый промпт субагенту из сессии Claude Code ставит метку «ответ ждём»
(`dsh_wait_expect`, владелец — `CLAUDE_CODE_SESSION_ID`). Снимает её любой забор ответа из
«входящих» (`dsh_board_inbox_take`: `ask --wait`, `answer`, `inbox --take`, сам ждун).
`dsh-board wait` — фоновый ждун: дожидается ответов по всем меткам своей сессии,
кладёт их в файлы, печатает итоги и выходит — выход и есть будильник. Застава на `Stop`
(`claude_hook_dsh_wait`) не отпускает ход, пока хоть одна метка осталась без живого ждуна:
общий ждёт все метки сессии, покарточный (`wait <карточка>`) — только названные в нём.

⚠ Раньше метку снимали только `ask --wait` и ждун: ответ, забранный `dsh-board answer`
(хоть субагентом, разбирающим чужие карточки), оставлял метку, и ждун висел до
`DSH_WAIT_MAX_AGE`, не будя оркестратора и по уже собранным ответам (2026-11-26).
⚠ Новый ждун сессии гасит прежнего (`_waiter_replace`): ждун, переживший перезапуск
харнеса, будить уже некого, а копились они по три-пять на сессию.

```bash
dsh-board wait --out-dir <каталог>      # фоном (run_in_background): разбудит по ответам
```

⚠ Обрыв (`turn/end interrupted`) — не ответ: его продолжит `revive --auto`, ждун ждёт дальше.
⚠ Ждёт по «входящим» на кэше списка карточек (60 с) — задержка до минуты, зато без тяжёлых
вызовов в цикле: они однажды уже уложили хост.
⚠ «Входящие» — это реестр `robots`; карточка, в реестр не попавшая, ответа там не покажет и
после конца круга. Поэтому под меткой ответ ищется и в журнале карточки напрямую
(`turn/end` позже метки) — иначе ждун не возвращался, даже когда жданная карточка уже
ответила (28.11, `d98f4f43`).
⚠⚠ Метка мертва, когда ответа карточки уже не будет: она в архиве, или, пережив
`DSH_WAIT_DEAD_GRACE`, не идёт, цели на доске не имеет, в очередь следующего захода ничего
не поставила и после метки не кончала круг (`dsh_wait_unreachable`). Круг, ждущий свободного
мотора, — не мёртвый и не готовый: он лежит в очереди заходов карточки, а в `running` и
журнале его ещё нет. Такие метки ждун и застава снимают сами (`dsh_wait_prune`), иначе одна
архивная карточка вешала всё ожидание на `DSH_WAIT_MAX_AGE` — 12 ч вместо ответа
(грабель 27–28.11, карточка `d98f4f43`).
⚠ Ответ карточки считается ответом на **нашу** метку, только если круг кончился позже неё:
у карточки мог лежать непрочитанным ответ прошлого круга, и ждун, забрав его по новой
метке, ещё и снял бы её — новый круг остался бы без watchdog'а, а сессия слышала бы
«ждать было нечего» при работающем субагенте (грабель 27.11, `ask` репликой).
"""
import fcntl
import json
import os
import re
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT, DshApiError

DSH_WAIT_FILE = 'dsh-board.expected.json'
DSH_WAIT_OWNER_ENV = 'CLAUDE_CODE_SESSION_ID'
DSH_WAIT_POLL = 20.0
# Метка старше — субагент или хост пропали насовсем; ждать её вечно нельзя.
DSH_WAIT_MAX_AGE = 12 * 3600
# Карточка спит, цели на доске и контента в очереди заходов нет — сколько даём встать
# кругу по прямой очереди промптов (`ask` репликой, `revive`): дольше — метку считать мёртвой.
DSH_WAIT_DEAD_GRACE = 600.0
# Расхождение часов метки (`time.time`) и журнала хоста: ответ чуть раньше метки — не повод
# зацикливать ждун на пропущенном ответе.
DSH_WAIT_CLOCK_SKEW = 2.0


def dsh_wait_expect(session: str, owner: str = None) -> None:
    """Пометить: из сессии `owner` (по умолчанию — из окружения) ждут ответа этой карточки."""
    owner = owner or os.environ.get(DSH_WAIT_OWNER_ENV) or ''
    if owner and session:
        _edit(lambda rows: rows.__setitem__(_key(session), {'owner': owner, 'at': time.time()}))


def dsh_wait_done(session: str) -> None:
    """Ответ карточки забран — метку снять."""
    _edit(lambda rows: rows.pop(_key(session), None))


def dsh_wait_pending(owner: str) -> list:
    """Карточки, ответа которых ждёт сессия `owner`; старые метки (`DSH_WAIT_MAX_AGE`) — мимо."""
    return sorted(dsh_wait_marks(owner))


def dsh_wait_marks(owner: str) -> dict:
    """Метки «ждём ответ», выставленные сессией: {карточка: {'owner', 'at'}}; старые — мимо.

    `at` (минута отдачи) нужен заставе «жди живого субагента» (`claude_gate_dsh_alive`),
    чтобы сказать человеку, сколько ждёт каждая карточка.
    """
    edge = time.time() - DSH_WAIT_MAX_AGE
    return {key: row for key, row in _read().items()
            if isinstance(row, dict) and row.get('owner') == owner and row.get('at', 0) > edge}


def dsh_wait_owner_of(session: str) -> str:
    """Сессия, которая сейчас ждёт ответа карточки (последней дала ей задание `ask`); нет
    свежей метки — пусто.

    ⚠ Не то же, что `dsh_saving_owner_of` («кто завёл»): другая сессия может отправить
    карточке новое задание — тогда ответ её, а заводила прежняя.
    """
    row = _read().get(_key(session))
    edge = time.time() - DSH_WAIT_MAX_AGE
    return str(row.get('owner') or '') if isinstance(row, dict) and row.get('at', 0) > edge else ''


def dsh_wait_key(session: str) -> str:
    """Ключ карточки в метках и во «входящих»: id без суффикса поколения (`-gN`).

    Им сверяют id карточки из `dsh_board_sessions` или с занятых моторов со списком меток;
    та же нормализация инлайном живёт в `dsh_board.py`.
    """
    return _key(session)


def dsh_wait_waiter_alive(owner: str) -> bool:
    """Жив ли хоть один процесс `dsh-board wait` этой сессии — общий или любого ряда.

    ⚠ Для решения «можно ли отпускать ход» этого мало: ждун `wait <карточка>` будит
    оркестратора только по своим карточкам — смотри `dsh_wait_waiter_gaps`.
    """
    return any(_waiter_alive(path) for path in _waiter_paths(owner))


def dsh_wait_waiter_gaps(owner: str, pending: list = None, *,
                         base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Какие метки сессии `owner` остались без живого ждуна — пробелы ожидания.

    Ждун без ряда (`wait` без карточек) покрывает **все** метки сессии; ждун
    `wait A B` — только карточки своего ряда: отработав первую (режим `first`), он
    остальных не будит. Заставе `Stop` и хуку `SessionStart` одних живых пидов мало:
    партия из трёх меток, где ждёт только одна карточка, молча отпустила бы ход, и
    два ответа ушли бы в никуда — ровно то, ради чего застава и появилась.

    Returns:
        Непокрытые метки (порядок `pending`); пусто — ждать нечего или всё под ждуном.
    """
    if pending is None:
        # Мёртвые метки (архив, уснувшая карточка без цели) ход не держат: без снятия
        # одна убранная карточка гоняла бы заставу по кругу вечно (28.11, `cff7d2ff`).
        dsh_wait_prune(owner, base=base)
    want = list(pending) if pending is not None else dsh_wait_pending(owner)
    if not want:
        return []
    for path, slot in _waiter_slots(owner):
        if not _waiter_alive(path):
            continue
        if not slot:
            return []                            # общий ждун покрывает все метки сессии
        want = [key for key in want if not _slot_has(slot, key)]
    return want


def dsh_wait_answered(sessions: list, marked_at: dict = None, *,
                      base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Из карточек `sessions` — те, чей ответ уже лежит непрочитанным; обрывы — мимо.

    Признак «ответ есть» один у ждуна и у заставы «жди живого субагента»
    (`claude_gate_dsh_alive`): карточка во «входящих» `dsh_board_inbox` (круг кончился,
    ответ не забран) и последний её `turn/end` — не `interrupted`: обрыв продолжит
    `revive --auto`, ответа нет.

    Args:
        marked_at: {карточка: минута метки «ждём ответ»}; с ней ответ, кончившийся
            **раньше** метки, — прошлый круг: его уже могли забрать, и к нашей задаче он
            не относится. Без неё — как раньше, любой непрочитанный.

    ⚠ «Входящие» недоступны — пустой список: пока хост молчит, про ответ не известно.
    ⚠ Сбой `dsh_session_history` не глотается — как в цикле ждуна до вынесения сюда.
    ⚠ С `marked_at` к «входящим» добавляется **журнал**: «входящие» показывают только
    карточки из реестра `robots`, а ждун ждёт карточку по метке — та могла в реестр и не
    попасть (ожидание `d98f4f43`, 28.11: карточка не в `robots`, круг кончился, ответа во
    «входящих» нет — ждун висел до `DSH_WAIT_MAX_AGE`). Ответом здесь считается `turn/end`
    позже метки, когда карточка не идёт и круг не забран (`turns` против реестра заборов).
    Без меток (`claude_gate_dsh_alive`, `rearm`) — прежний путь, только «входящие»:
    для них реестр и есть источник.
    """
    from dsh_.dsh_board import (dsh_board_inbox, dsh_board_inbox_taken,  # ленивый: цикл импорта
                                dsh_board_turns)
    from dsh_.dsh_session import dsh_session_history, dsh_session_list, dsh_session_title

    want = {_key(key) for key in sessions or []}
    marked_at = marked_at or {}

    try:
        rows = [row for row in dsh_board_inbox(base=base) if _key(row['session']) in want]
    except DshApiError:
        rows = []
    out = []
    for row in rows:
        events = dsh_session_history(row['session'], 2, base=base)
        if _interrupted(events):
            continue
        at = marked_at.get(_key(row['session'])) or 0
        end = _turn_end_at(events)
        if at and end and end < at - DSH_WAIT_CLOCK_SKEW:
            continue                     # ответ прошлого круга: нашей метки он не касается
        out.append(row)

    rest = want - {_key(row['session']) for row in out}
    rest = {key for key in rest if (marked_at.get(key) or 0) > 0}
    if not rest:
        return out
    try:
        items = {_key(item.get('sessionId') or ''): item for item in dsh_session_list(base=base)}
    except (DshApiError, OSError):
        return out                       # хост молчит — про ответ не известно
    for key in sorted(rest):
        item = items.get(key)
        if item is None or item.get('running'):
            continue                     # карточка круг ещё не кончила (или её ещё нет)
        turns = dsh_board_turns(item)
        if turns <= dsh_board_inbox_taken(key):
            continue                     # кругов после забранных нет — того, нашего, ещё нет
        try:
            events = dsh_session_history(key, 2, base=base)
        except (DshApiError, OSError):
            continue
        if _interrupted(events):
            continue                     # обрыв продолжит `revive --auto`
        if _turn_end_at(events) < marked_at[key] - DSH_WAIT_CLOCK_SKEW:
            continue                     # кончился раньше метки — прошлый круг
        out.append({'session': key, 'title': dsh_session_title(item), 'turns': turns,
                    'brief': ''})
    return out


def dsh_wait_unreachable(sessions: list, marked_at: dict = None, *,
                         base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Карточки, чьего ответа ждать некому и незачем: метка мертва.

    Живая надежда на ответ — это круг, который **идёт**, цель на доске, которая ещё
    отработает (ждёт мотора, в очереди), круг, кончившийся **после** метки, или контент,
    поставленный в **очередь заходов** карточки: когда все моторы заняты чужим, принятый
    промпт лежит в проекции `inbox` (`next-step`/`next-turn`), а карточка выглядит мёртвой
    — `running` ложь, `turns` прежний, журнала круга ещё нет (28.11: четыре мотора таскали
    чужие карточки, жданная стояла в очереди часами). Архивная карточка не ответит
    никогда; карточка без цели, без очереди и без конца круга после метки, пережившая
    `DSH_WAIT_DEAD_GRACE`, — тоже.

    ⚠ Карточки, которой нет в списке хоста (завелась минуту назад, кэш списка молчит), —
    живые: незнание хоста не повод хоронить метку; то же при недоступном хосте — пустой список.
    """
    from dsh_.dsh_board import dsh_board_state, dsh_board_turns  # ленивый: цикл импорта
    from dsh_.dsh_session import dsh_session_history, dsh_session_list

    keys = sorted({_key(session) for session in sessions or [] if session})
    if not keys:
        return []
    try:
        state = dsh_board_state(base=base)
        items = {_key(item.get('sessionId') or ''): item for item in dsh_session_list(base=base)}
    except (DshApiError, OSError):
        return []
    archived = {_key(session) for session in state.get('archived') or []}
    queued = {_key(goal.get('session') or '') for goal in state.get('goals') or []
              if goal.get('status') in ('active', 'paused', 'blocked')}
    now, dead = time.time(), []
    for key in keys:
        if key in archived:
            dead.append(key)
            continue
        item = items.get(key)
        if key in queued or item is None or item.get('running') or _queue_waiting(item):
            continue                     # круг идёт, в очереди или ещё не дорос до решения
        at = (marked_at or {}).get(key) or 0
        if not at or now - at < DSH_WAIT_DEAD_GRACE:
            continue
        if dsh_board_turns(item):
            try:
                end = _turn_end_at(dsh_session_history(key, 2, base=base))
            except (DshApiError, OSError):
                continue
            if end > at - DSH_WAIT_CLOCK_SKEW:
                continue                 # после метки был круг — его заберёт `dsh_wait_answered`
        dead.append(key)
    return dead


def dsh_wait_prune(owner: str, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Снять метки сессии, ответа которых уже не будет; вернуть их ключи.

    Мёртвая метка живее живой: без снятия она держит ждун до `DSH_WAIT_MAX_AGE` и не
    отпускает ход на `Stop` (`dsh_wait_waiter_gaps`) — оркестратор в петле «субагент ещё
    работает» по карточке, убранной в архив руками или `gc`.
    """
    marks = dsh_wait_marks(owner)
    if not marks:
        return []
    dead = dsh_wait_unreachable(list(marks), {k: row.get('at') or 0 for k, row in marks.items()},
                                base=base)
    for key in dead:
        dsh_wait_done(key)
    return dead


def dsh_wait_run(owner: str, out_dir: str, timeout: float = 6 * 3600, sessions: list = None,
                 first: bool = False, *, slot: str = '', base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Дождаться ответов — всех меток сессии или названных карточек: в файлы, метки снять.

    Args:
        owner: сессия Claude Code, чьи метки разбираются (по неё же и будильник).
        out_dir: каталог для полных ответов.
        timeout: потолок всего ожидания, с.
        sessions: ждать **эти** карточки, а не все метки сессии; карточка без метки тоже
            годится — ответ ищется по «входящим».
        first: выйти на первом же забранном ответе — так одна карточка будит оркестратора
            сама, не дожидаясь самой медленной в партии.
        slot: разъезд для нескольких ждунов одной сессии (по одному на карточку): у каждого
            свой файл pid, гасят они только своего двойника.

    ⚠ Без `first` ждун выходит, когда кончились все его карточки: партия из четырёх задач
    с кругами 12, 20, 40 и 60 мин будила оркестратора один раз — на 60-й минуте, и три
    готовых ответа ждали своей очереди ещё 8, 40 и 48 мин (замер 24–27.11: 18,6 ч такой
    выдержки на 107 ответах). Поэтому `dsh-board wait <карточка>` — по процессу на карточку.
    ⚠ Ответ, уже забранный кем-то другим, в «входящих» не лежит. Безымянный ждун перечитывает
    метки каждый виток — снятая чужим забором он пустит сразу; названному карточка фиксирована
    на старте — такую он дождётся разве что до `timeout`.

    Returns:
        `[{session, title, out, brief}]` — забранные ответы; пусто — ждать было нечего
        или вышел срок (тогда метки остаются).
    """
    from dsh_.dsh_board import dsh_board_box_clear, dsh_board_brief, dsh_board_inbox_take  # ленивый: цикл импорта
    from dsh_.dsh_session import dsh_session_answer_last
    os.makedirs(out_dir, exist_ok=True)
    _waiter_replace(owner, slot)
    with open(_waiter_path(owner, slot), 'w', encoding='utf-8') as handle:
        handle.write(str(os.getpid()))
    named = sessions is not None
    want = [dsh_wait_key(key) for key in sessions] if named else dsh_wait_pending(owner)
    taken, deadline = [], time.time() + timeout
    try:
        while want and time.time() < deadline:
            # `at` меток — по нему ответ сверяется с меткой: ответом считается лишь круг,
            # кончившийся позже неё (у карточки мог лежать непрочитанным прошлый круг).
            rows = _read()
            marked_at = {key: (rows.get(key) or {}).get('at') or 0
                         for key in want if isinstance(rows.get(key), dict)}
            got = set()
            for row in dsh_wait_answered(want, marked_at=marked_at, base=base):
                session = row['session']
                answer = dsh_session_answer_last(session, base=base) or ''
                path = os.path.join(out_dir, f'{dsh_wait_key(session)}.md')
                with open(path, 'w', encoding='utf-8', errors='replace') as out:
                    out.write(answer)
                dsh_board_inbox_take(session, row['turns'])  # снимает и метку
                dsh_board_box_clear(session, base=base)
                taken.append({'session': session, 'title': row['title'], 'out': path,
                              'brief': dsh_board_brief(answer)})
                got.add(dsh_wait_key(session))
                if first:
                    break
            if got:
                want = [key for key in want if key not in got]
            if first and taken:
                break
            # Мёртвые метки снимаем и здесь, не дожидаясь 12 ч: архивная карточка или
            # уснувшая без цели вешала бы весь отряд (`d98f4f43`, 27–28.11).
            dead = set(dsh_wait_unreachable(want, marked_at, base=base))
            for key in dead:
                dsh_wait_done(key)
            if dead:
                want = [key for key in want if key not in dead]
            if not named:
                # Метки безымянного ждуна живут в файле: их мог снять чужой забор ответа
                # (`answer`, `inbox --take`) или добавить новую — перечитываем каждый
                # виток, как было до покарточных ждунов. Снятую метку не перечитав —
                # висели бы до `timeout` (грабель 2026-11-26), не дождавшись новой —
                # никогда.
                want = dsh_wait_pending(owner)
            if want:
                time.sleep(DSH_WAIT_POLL)
    finally:
        # Файл чужого ждуна (сменившего этот через `_waiter_replace`) не трогаем.
        try:
            with open(_waiter_path(owner, slot), encoding='utf-8') as handle:
                mine = handle.read().strip() == str(os.getpid())
            if mine:
                os.unlink(_waiter_path(owner, slot))
        except OSError:
            pass
    return taken


def _waiter_replace(owner: str, slot: str = '') -> None:
    """Погасить прежнего ждуна **этого же ряда** этой сессии: будит он только свой процесс.

    Новый ждун той же сессии появляется, когда прежний пережил перезапуск харнеса (его
    будильник уже никуда не придёт) или когда ход запустил второй. Без замены они копились
    часами — по три-пять процессов на сессию. ⚠ Ряд (`slot`) — часть имени: ждуны разных
    карточек друг друга не гасят, иначе второй `wait <карточка>` снял бы первого.
    Гасим только процесс `dsh-board wait`: pid мог достаться чужому процессу.
    """
    try:
        with open(_waiter_path(owner, slot), encoding='utf-8') as handle:
            pid = int(handle.read().strip())
        if pid == os.getpid():
            return
        with open(f'/proc/{pid}/cmdline', 'rb') as handle:
            cmdline = handle.read().replace(b'\0', b' ').decode('utf-8', 'replace')
        if 'dsh-board' in cmdline and ' wait' in cmdline:
            os.kill(pid, 15)
    except (OSError, ValueError):
        pass


def _interrupted(events: list) -> bool:
    ends = [event for event in events if event.get('type') == 'turn/end']
    reason = ((ends[-1].get('data') or {}).get('reason') or {}) if ends else {}
    return isinstance(reason, dict) and reason.get('kind') == 'interrupted'


def _queue_waiting(item: dict) -> bool:
    """Стоит ли в очереди следующего захода карточки контент (проекция `inbox` хоста)?

    Пока все моторы заняты чужим, принятый промпт лежит в `next-step`/`next-turn` и ждёт
    свободного мотора: `running` — ложь, `turns` — прежний, круга в журнале ещё нет.
    Для ждуна и заставы это **pending**: круг не кончен и не мёртв (28.11).
    """
    queue = ((item.get('projections') or {}).get('values') or {}).get('inbox') or {}
    return bool(queue.get('next-turn') or queue.get('next-step'))


def _turn_end_at(events: list) -> float:
    """Число эпохи последнего `turn/end` из хвоста журнала, с; записи нет — 0 (не выдумывать).

    Разбор времени — как `_event_clock` в `dsh_session.py` (хост пишет мс); сюда не зовём:
    через чужое приватное ход — грабель на первый же рефакторинг того слоя.
    """
    ends = [event for event in events or [] if event.get('type') == 'turn/end']
    if not ends:
        return 0.0
    try:
        value = float(ends[-1].get('time') or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 1000 if value > 1e11 else value


def _key(session: str) -> str:
    return re.sub(r'-g\d+$', '', session or '')


def _home(name: str) -> str:
    return os.path.join(os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT), name)


def _waiter_path(owner: str, slot: str = '') -> str:
    """Файл pid ждуна. Ряд (`slot`) — часть имени: у сессии их теперь несколько, по одному
    на карточку (`wait <карточка>`), и в имени они различаются хвостом после имени сессии."""
    tag = re.sub(r'[^0-9a-zA-Z-]', '', owner)
    if slot:
        tag += '.' + re.sub(r'[^0-9a-zA-Z-]', '', slot)
    return _home(f'dsh-board.waiter.{tag}.pid')


def _waiter_paths(owner: str) -> list:
    """Все файлы ждунов сессии — общего ряда и покарточных."""
    return [path for path, _slot in _waiter_slots(owner)]


def _waiter_slots(owner: str) -> list:
    """Файлы ждунов сессии как [(файл, ряд)]: ряд `''` — общий ждун, он покрывает всё."""
    tag = re.sub(r'[^0-9a-zA-Z-]', '', owner)
    prefix, out = f'dsh-board.waiter.{tag}', []
    try:
        with os.scandir(_home('')) as scan:
            for entry in scan:
                if not entry.name.endswith('.pid') or not entry.name.startswith(f'{prefix}.'):
                    continue
                slot = entry.name[len(prefix) + 1:-len('.pid')]
                out.append((entry.path, slot))
    except OSError:
        return out
    return sorted(out)


def _waiter_alive(path: str) -> bool:
    """Жив ли процесс из файла pid ждуна."""
    try:
        with open(path, encoding='utf-8') as handle:
            os.kill(int(handle.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def _slot_has(slot: str, key: str) -> bool:
    """Карточка в этом ряду: ряд — карточки через `-`, сверяем ключ целиком, в обрамлении `-`.

    ⚠ Ряд склеен теми же дефисами, что внутри ключей (`continuous-<uuid>`), так что
    совпадение по куску ключа невозможно — нужен весь ключ между дефисами; теоретический
    ложный случай — ключ целиком, угодивший ровно на шов двух чужих карточек ряда.
    """
    clean = re.sub(r'[^0-9a-zA-Z-]', '', key)
    return clean and f'-{clean}-' in f'-{slot}-'


def _read() -> dict:
    try:
        with open(_home(DSH_WAIT_FILE), encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _edit(change) -> None:
    """Правка меток под файловым замком: пишут потоки fanout, ждун и чужие процессы."""
    path = _home(DSH_WAIT_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = _read()
        change(rows)
        # Просроченные метки никто уже не ждёт (`dsh_wait_marks` их пропускает) — не копим.
        edge = time.time() - DSH_WAIT_MAX_AGE
        for key in [key for key, row in rows.items()
                    if not isinstance(row, dict) or row.get('at', 0) <= edge]:
            rows.pop(key)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(rows, handle)
        os.replace(tmp, path)

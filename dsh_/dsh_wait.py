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


def dsh_wait_waiter_gaps(owner: str, pending: list = None) -> list:
    """Какие метки сессии `owner` остались без живого ждуна — пробелы ожидания.

    Ждун без ряда (`wait` без карточек) покрывает **все** метки сессии; ждун
    `wait A B` — только карточки своего ряда: отработав первую (режим `first`), он
    остальных не будит. Заставе `Stop` и хуку `SessionStart` одних живых пидов мало:
    партия из трёх меток, где ждёт только одна карточка, молча отпустила бы ход, и
    два ответа ушли бы в никуда — ровно то, ради чего застава и появилась.

    Returns:
        Непокрытые метки (порядок `pending`); пусто — ждать нечего или всё под ждуном.
    """
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


def dsh_wait_answered(sessions: list, *, base: str = DSH_API_ADDR_DEFAULT) -> list:
    """Из карточек `sessions` — те, чей ответ уже лежит непрочитанным; обрывы — мимо.

    Признак «ответ есть» один у ждуна и у заставы «жди живого субагента»
    (`claude_gate_dsh_alive`): карточка во «входящих» `dsh_board_inbox` (круг кончился,
    ответ не забран) и последний её `turn/end` — не `interrupted`: обрыв продолжит
    `revive --auto`, ответа нет.

    ⚠ «Входящие» недоступны — пустой список: пока хост молчит, про ответ не известно.
    ⚠ Сбой `dsh_session_history` не глотается — как в цикле ждуна до вынесения сюда.
    """
    from dsh_.dsh_board import dsh_board_inbox  # ленивый: цикл импорта
    from dsh_.dsh_session import dsh_session_history

    want = {_key(key) for key in sessions or []}

    try:
        rows = [row for row in dsh_board_inbox(base=base) if _key(row['session']) in want]
    except DshApiError:
        return []

    return [row for row in rows if not _interrupted(dsh_session_history(row['session'], 2, base=base))]


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
            got = set()
            for row in dsh_wait_answered(want, base=base):
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

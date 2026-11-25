"""Сколько процентов тарифа Claude Code сберегли субагенты DSH — числом для статусной строки.

Работа субагента — это токены, которые оркестратор (Claude Code) иначе прочёл и написал бы
сам. Хост DSH знает их счёт (`tokenUsage` карточки), а Claude Code в статусную строку отдаёт
занятость окон подписки (`rate_limits.five_hour/seven_day.used_percentage`). Мост между
ними — **калибровка**: сколько взвешенных токенов сам Claude потратил в текущем окне
(транскрипты `~/.claude/projects/**.jsonl`), делённое на процент окна, = цена одного
процента. Экономия в процентах = (работа субагентов − цена делегирования) / цена процента.

```bash
dsh-board saving                         # по сессии Claude, из которой зовут, и по всем
dsh-board working --line --session <id>  # «DSH 1/4 (-5%): …» — для статусной строки
```

⚠ Цифра — оценка, а не счёт Anthropic:
- веса входа/выхода/кэша взяты из прайса API (`DSH_SAVING_WEIGHTS`); как именно подписка
  взвешивает чтение кэша, не опубликовано;
- транскрипты видны только с этой машины: расход того же аккаунта с других машин делает
  процент «дороже», а экономию — **меньше**, не больше;
- субагент считается столько, сколько потратил сам; оркестратор на той же работе гонял бы
  весь свой контекст на каждом шаге — настоящая экономия выше.
⚠ На машине бывают сессии **разных аккаунтов**: аккаунт узнаётся по сбросу недельного окна
(`seven_day.resets_at`, с допуском `DSH_SAVING_ACCOUNT_DRIFT` — Anthropic сдвигает сброс на
часы у того же аккаунта), транскрипты сессий с чужим сбросом в калибровку не идут.
⚠ Процент окна приходит целым числом: пока окно занято меньше `DSH_SAVING_MIN_PCT`,
калибровка неточна, и берётся прошлая.
"""
import glob
import json
import os
import re
import time
from datetime import datetime

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DSH_API_HOME_DEFAULT
from dsh_.dsh_session import dsh_session_list

# Цена токена относительно входного — по прайсу Anthropic (одинакова для всех моделей).
DSH_SAVING_WEIGHTS = {'input': 1.0, 'output': 5.0, 'cache_read': 0.1, 'cache_write': 1.25}
DSH_SAVING_WINDOWS = {'five_hour': 5 * 3600, 'seven_day': 7 * 86400}
# Снимки `rate_limits` кладёт статусная строка (~/.claude/statusline.sh), файл на сессию:
# на одной машине бывают сессии разных аккаунтов, и общий файл они перетирали друг другу.
DSH_SAVING_RATE_DIR = '~/.cache/claude-rate'
DSH_SAVING_RATE_MAX_AGE = 7 * 86400
DSH_SAVING_CALIBRATION_FILE = '~/.cache/dsh-saving.calibration.json'
DSH_SAVING_CALIBRATION_TTL = 300
DSH_SAVING_REPORT_FILE = '~/.cache/dsh-saving.report.json'
DSH_SAVING_REPORT_TTL = 60
# Меньше — калибровка шумная (процент целый, окно свежее), берётся прошлая того же аккаунта.
DSH_SAVING_MIN_PCT = 10
# Anthropic сдвигает недельный сброс у того же аккаунта на часы (было 1795777200 →
# 1795780800): два `seven_day.resets_at` с расхождением меньше этого — один аккаунт.
DSH_SAVING_ACCOUNT_DRIFT = 86400
# Точка отсчёта метки: экономия сессии на начало текущего 5-часового окна её аккаунта.
DSH_SAVING_BASELINE_FILE = '~/.cache/dsh-saving.baseline.json'
DSH_SAVING_TRANSCRIPTS = '~/.claude/projects/**/*.jsonl'
# Чей субагент: сессия Claude Code, из которой завели карточку (в каталоге хоста DSH).
DSH_SAVING_OWNERS = 'dsh-board.owners.json'
DSH_SAVING_OWNER_ENV = 'CLAUDE_CODE_SESSION_ID'
# Цена делегирования для оркестратора: промпт он пишет (выход), выжимку читает (вход).
DSH_SAVING_CHARS_PER_TOKEN = 3.0
DSH_SAVING_BRIEF_CHARS = 700


def dsh_saving_owner_mark(session: str, owner: str = None) -> None:
    """Пометить карточку сессией Claude Code, которая её завела (по умолчанию — из окружения).

    Без пометки экономию не разложить по сессиям Claude: хост про оркестратора не знает.
    Пустой владелец (зовут не из Claude Code) — ничего не пишется.
    """
    owner = owner or os.environ.get(DSH_SAVING_OWNER_ENV) or ''
    if not owner or not session:
        return
    owners = _owners_read()
    owners.setdefault(_key(session), {'owner': owner, 'at': time.time(), 'sent': 0})
    _json_write(_dsh_home(DSH_SAVING_OWNERS), owners)


def dsh_saving_owner_of(session: str) -> str:
    """Сессия Claude Code, которая завела карточку субагента (`dsh_saving_owner_mark`); нет — пусто."""
    return str((_owners_read().get(_key(session)) or {}).get('owner') or '')


def dsh_saving_sent(session: str, text: str) -> None:
    """Учесть промпт, отправленный карточке: его длина — расход оркестратора на делегирование."""
    owners = _owners_read()
    row = owners.get(_key(session))
    if row is None:
        return
    row['sent'] = int(row.get('sent') or 0) + len(text or '')
    _json_write(_dsh_home(DSH_SAVING_OWNERS), owners)


def dsh_saving_units(usage: dict) -> float:
    """Взвешенные токены карточки DSH (`tokenUsage`) — в единицах цены входного токена."""
    weight = DSH_SAVING_WEIGHTS
    return (int(usage.get('uncachedInputTokens') or 0) * weight['input']
            + int(usage.get('outputTokens') or 0) * weight['output']
            + int(usage.get('cacheReadTokens') or 0) * weight['cache_read']
            + int(usage.get('cacheWriteTokens') or 0) * weight['cache_write'])


def dsh_saving_account_same(left: str, right: str) -> bool:
    """Один ли это аккаунт подписки по двум значениям `seven_day.resets_at`.

    Точное равенство здесь не работает: Anthropic сдвигает недельный сброс у того же
    аккаунта на часы — один аккаунт, если значения расходятся меньше чем на
    `DSH_SAVING_ACCOUNT_DRIFT` секунд (разница в дни — уже другой аккаунт).
    ⚠ Пустой/нулевой сброс не совпадает ни с чем: это «аккаунт неизвестен», а не сброс.
    """
    a, b = _account_num(left), _account_num(right)
    return bool(a) and bool(b) and abs(a - b) < DSH_SAVING_ACCOUNT_DRIFT


def dsh_saving_calibrate(owner: str = None, force: bool = False) -> dict:
    """Цена 1% окна подписки во взвешенных токенах Claude — по окнам `five_hour`/`seven_day`.

    Args:
        owner: сессия Claude Code, чей аккаунт калибровать; пусто — по свежайшему снимку.

    Returns:
        `{account, five_hour: {per_pct, used_pct, units}, seven_day: {...}, at}`; окно без
        данных — `per_pct: None`.
    """
    snapshots = _snapshots()
    mine = snapshots.get(owner) if owner else None
    if mine is None and snapshots:
        mine = max(snapshots.values(), key=lambda row: float(row.get('at') or 0))
    limits = (mine or {}).get('rate_limits') or {}
    account = str((limits.get('seven_day') or {}).get('resets_at') or '')
    path = os.path.expanduser(DSH_SAVING_CALIBRATION_FILE)
    store = _json_read(path) or {}
    # Сброс недельного окна мог уехать на часы у того же аккаунта — калибровка тогда
    # берётся из ближайшей записи в пределах дрейфа, а новая пишется под новым ключом.
    cached = _calibration_find(store, account)
    if not force and cached and time.time() - float(cached.get('at') or 0) < DSH_SAVING_CALIBRATION_TTL:
        return cached
    foreign = {sid for sid, row in snapshots.items()
               if not dsh_saving_account_same(
                   str(((row.get('rate_limits') or {}).get('seven_day') or {}).get('resets_at') or ''), account)}
    result = {'account': account, 'at': time.time()}
    for window, span in DSH_SAVING_WINDOWS.items():
        limit = limits.get(window) or {}
        used = float(limit.get('used_percentage') or 0)
        previous = cached.get(window) or {}
        if not limit.get('resets_at') or used < DSH_SAVING_MIN_PCT:
            result[window] = {**previous, 'used_pct': used, 'stale': True} if previous.get('per_pct') \
                else {'per_pct': None, 'used_pct': used, 'units': 0}
            continue
        units = _claude_units(float(limit['resets_at']) - span, foreign)
        result[window] = {'per_pct': units / used, 'used_pct': used, 'units': units}
    store[account] = result
    _json_write(path, store)
    return result


def dsh_saving_report(owner: str = None, account_of: str = None, *,
                      base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Экономия субагентов: взвешенные токены и проценты окон подписки.

    Args:
        owner: id сессии Claude Code — считать только её карточки; пусто — все агентские.
        account_of: сессия, по чьему аккаунту переводить в проценты; пусто — `owner`.

    Returns:
        `{owner, sessions, worked, overhead, saved, five_hour_pct, seven_day_pct}`;
        процент — None, пока калибровки нет.
    """
    owners = _owners_read()
    if owner:
        keys = {key for key, row in owners.items() if row.get('owner') == owner}
    else:
        from dsh_.dsh_board import dsh_board_state  # ленивый: dsh_board сам зовёт этот модуль
        keys = set(dsh_board_state(base=base).get('robots') or []) | set(owners)
    worked = overhead = 0.0
    count = 0
    if keys:
        for item in dsh_session_list(base=base):
            key = _key(item.get('sessionId') or '')
            if key not in keys:
                continue
            values = (item.get('projections') or {}).get('values') or {}
            usage = values.get('tokenUsage') or {}
            if not usage:
                continue
            count += 1
            worked += dsh_saving_units(usage)
            turns = int((values.get('sessionStats') or {}).get('turns') or 0)
            overhead += _overhead_units(int((owners.get(key) or {}).get('sent') or 0), turns)
    saved = max(0.0, worked - overhead)
    calibration = dsh_saving_calibrate(account_of or owner)
    report = {'owner': owner or '', 'sessions': count, 'worked': round(worked),
              'overhead': round(overhead), 'saved': round(saved)}
    for window in DSH_SAVING_WINDOWS:
        per_pct = (calibration.get(window) or {}).get('per_pct')
        report[f'{window}_pct'] = round(saved / per_pct, 1) if per_pct else None
    return report


def dsh_saving_badge(owner: str, *, base: str = DSH_API_ADDR_DEFAULT) -> str:
    """Метка «(-5%)» для статусной строки — сколько субагенты сберегли **в текущем** 5-часовом окне.

    Считается прирост экономии от начала окна (`dsh_saving_refresh` запоминает точку
    отсчёта при смене окна или аккаунта), делённый на цену процента этого же окна.
    ⚠ Не вся накопленная экономия: за день она легко «больше 100%» одного окна, и после
    `/login` на другой аккаунт делилась бы на чужую цену процента (было «-212%»).
    Калибровка из прошлого окна — метка с `~`.

    Ответ кэшируется на `DSH_SAVING_REPORT_TTL` секунд по сессии: статусная строка зовёт
    каждую секунду. Экономии нет или калибровки нет — пустая строка.
    """
    if not owner:
        return ''
    path = os.path.expanduser(DSH_SAVING_REPORT_FILE)
    row = (_json_read(path) or {}).get(owner) or {}
    if time.time() - float(row.get('at') or 0) >= DSH_SAVING_REPORT_TTL:
        # ⚠⚠ Сперва застолбить срок, потом считать — и считать в отдельном процессе.
        # `session/list` под нагрузкой дольше таймаута статусной строки (8 с): её убивали
        # до записи кэша, через секунду она начинала снова — и сама держала хост под
        # непрерывным потоком тяжёлых запросов. Ответ строки — прошлое значение.
        _report_store(owner, {**row, 'at': time.time()})
        _report_spawn(owner, base)
    pct = row.get('window_pct')
    if not pct or pct < 0.05:
        return ''
    mark = '~' if row.get('stale') else ''
    return f"(-{mark}{pct:.0f}%)" if pct >= 1 else f"(-{mark}{pct:.1f}%)"


def dsh_saving_refresh(owner: str, *, base: str = DSH_API_ADDR_DEFAULT) -> dict:
    """Пересчитать экономию сессии и положить в кэш метки — фоновая половина `badge`.

    Окно — аккаунт и сброс 5-часового окна из снимка сессии; новое окно — новая точка
    отсчёта (экономия на этот момент), метка считает прирост от неё. Дрейф недельного
    сброса в пределах `DSH_SAVING_ACCOUNT_DRIFT` окном не считается — точка отсчёта живёт.
    """
    row = {**dsh_saving_report(owner, base=base), 'at': time.time()}
    limits = ((_snapshots().get(owner) or {}).get('rate_limits') or {})
    window = f"{(limits.get('seven_day') or {}).get('resets_at')}/{(limits.get('five_hour') or {}).get('resets_at')}"
    path = os.path.expanduser(DSH_SAVING_BASELINE_FILE)
    baselines = _json_read(path) or {}
    mark = baselines.get(owner) or {}
    if _window_same(mark.get('window'), window):
        if mark.get('window') != window:
            # Тот же аккаунт, недельный сброс сдвинулся — точка отсчёта остаётся, ключ свежим.
            mark['window'] = window
            baselines[owner] = mark
            _baselines_write(path, baselines)
    else:
        mark = {'window': window, 'saved': row['saved'], 'at': time.time()}
        baselines[owner] = mark
        _baselines_write(path, baselines)
    calibration = dsh_saving_calibrate(owner).get('five_hour') or {}
    per_pct = calibration.get('per_pct')
    gained = max(0, row['saved'] - int(mark.get('saved') or 0))
    row.update(window_saved=gained, stale=bool(calibration.get('stale')),
               window_pct=round(gained / per_pct, 1) if per_pct else None)
    _report_store(owner, row)
    return row


def _report_store(owner: str, row: dict) -> None:
    path = os.path.expanduser(DSH_SAVING_REPORT_FILE)
    now = time.time()
    cache = {key: value for key, value in (_json_read(path) or {}).items()
             if now - float(value.get('at') or 0) < 86400}
    cache[owner] = row
    _json_write(path, cache)


def _report_spawn(owner: str, base: str) -> None:
    """Пересчёт отдельным процессом, отвязанным от вызывающего: таймаут строки его не убьёт."""
    import subprocess
    import sys
    code = ('import sys; from dsh_.dsh_saving import dsh_saving_refresh; '
            'dsh_saving_refresh(sys.argv[1], base=sys.argv[2])')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, 'PYTHONPATH': root + os.pathsep + os.environ.get('PYTHONPATH', '')}
    subprocess.Popen([sys.executable, '-c', code, owner, base], env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _claude_units(since: float, foreign: set = frozenset()) -> float:
    """Взвешенные токены ответов Claude с момента `since` — по транскриптам этой машины.

    Транскрипт сессии `foreign` (чужой аккаунт) пропускается — и свой `<id>.jsonl`, и её
    подагенты в `<id>/subagents/`. Один ответ пишется в транскрипт несколькими строками
    с одним `message.id` — берётся последняя.
    """
    seen = {}
    for path in glob.glob(os.path.expanduser(DSH_SAVING_TRANSCRIPTS), recursive=True):
        parts = set(os.path.normpath(path).split(os.sep)) | {os.path.basename(path)[:-len('.jsonl')]}
        if foreign & parts:
            continue
        try:
            if os.path.getmtime(path) < since:
                continue
            with open(path, encoding='utf-8', errors='ignore') as handle:
                for line in handle:
                    if '"usage"' not in line:
                        continue
                    _usage_take(line, since, seen)
        except OSError:
            continue
    weight = DSH_SAVING_WEIGHTS
    return sum(int(u.get('input_tokens') or 0) * weight['input']
               + int(u.get('output_tokens') or 0) * weight['output']
               + int(u.get('cache_read_input_tokens') or 0) * weight['cache_read']
               + int(u.get('cache_creation_input_tokens') or 0) * weight['cache_write']
               for u in seen.values())


def _usage_take(line: str, since: float, seen: dict) -> None:
    try:
        entry = json.loads(line)
        message = entry.get('message') or {}
        usage = message.get('usage')
        if not usage or not message.get('id'):
            return
        stamp = datetime.fromisoformat(str(entry.get('timestamp')).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return
    if stamp >= since:
        seen[message['id']] = usage


def _snapshots() -> dict:
    """Снимки `rate_limits` живых сессий: `{session_id: {rate_limits, at}}`, старые — мимо."""
    rows = {}
    for path in glob.glob(os.path.join(os.path.expanduser(DSH_SAVING_RATE_DIR), '*.json')):
        row = _json_read(path) or {}
        if row.get('session_id') and time.time() - float(row.get('at') or 0) < DSH_SAVING_RATE_MAX_AGE:
            rows[row['session_id']] = row
    return rows


def _overhead_units(sent_chars: int, turns: int) -> float:
    weight = DSH_SAVING_WEIGHTS
    return (sent_chars / DSH_SAVING_CHARS_PER_TOKEN * weight['output']
            + turns * DSH_SAVING_BRIEF_CHARS / DSH_SAVING_CHARS_PER_TOKEN * weight['input'])


def _calibration_find(store: dict, account: str) -> dict:
    """Запись калибровки этого аккаунта: точный ключ, иначе ближайшая в пределах дрейфа."""
    if store.get(account):
        return store[account]
    found, gap = None, None
    for key, row in store.items():
        if not dsh_saving_account_same(key, account):
            continue
        delta = abs(_account_num(key) - _account_num(account))
        if gap is None or delta < gap:
            found, gap = row, delta
    return found or {}


def _window_same(old: str, new: str) -> bool:
    """То же ли 5-часовое окно: сброс `five_hour` тот же, недельный — в пределах дрейфа."""
    new_seven, _, new_five = str(new or '').partition('/')
    old_seven, _, old_five = str(old or '').partition('/')
    return old_five == new_five and dsh_saving_account_same(old_seven, new_seven)


def _baselines_write(path: str, baselines: dict) -> None:
    """Записать точки отсчёта, выбросив те, что старше недели."""
    _json_write(path, {key: value for key, value in baselines.items()
                       if time.time() - float(value.get('at') or 0) < 7 * 86400})


def _account_num(value) -> float:
    """`resets_at` числом для сопоставления аккаунта; пустое/нечисловое — 0.0 («ни с чем»)."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _key(session: str) -> str:
    return re.sub(r'-g\d+$', '', session or '')


def _owners_read() -> dict:
    return _json_read(_dsh_home(DSH_SAVING_OWNERS)) or {}


def _dsh_home(name: str) -> str:
    return os.path.join(os.path.expanduser(os.environ.get('DSH_HOME') or DSH_API_HOME_DEFAULT), name)


def _json_read(path: str):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _json_write(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False)
    os.replace(tmp, path)

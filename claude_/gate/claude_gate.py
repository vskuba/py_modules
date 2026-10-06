"""
Когда заставе молчать: память о придирках и спускной клапан — одним местом на все заставы.

Застава стоит на каждом ходе агента, и вопрос «что проверить» — её собственный.
Общий у всех заставы другой вопрос: **придираться ли сейчас вообще**. Ответ на него
складывается из трёх вещей, и все три одинаковы у любой заставы:

    снята клапаном      → молчать, выкладка бывает срочной
    бюджет исчерпан     → молчать, второй раз о том же не говорят
    эпизод закрыт       → забыть, чтобы завтра сказать снова

Предмет этого модуля — **политика прерывания**, а не проверка: что именно смотрят,
знают `tool_instead`, `deploy_precheck` и правила вёрстки, каждый у себя. Разбор ввода
хука и коды возврата — этажом ниже, в `claude_/claude_hook.py`.

## ⚠⚠ Это уже писали дважды, и копии разошлись

`tool_gate` помнил `{путь: epoch}` — голое число, — а `design_gate` `{сессия: {asks,
at}}` — запись. Из-за числа счётчик придирок не помещался вовсе, а чистка протухшего
стояла у одного при записи, у другого при чтении. Здесь взята запись: счётчик без
отметки времени не гаснет никогда, и эпизод, один раз отпущенный, оставался бы
прощённым навсегда — даже после новых правок на следующий день.

Часы параметром (`now`) — оттуда же: без них проверить «назавтра придирка вернулась»
нечем, кроме как ждать сутки.

## Как этим пользуются

    if claude_gate_off(TOOL_GATE_PASS):
        return ''

    if claude_gate_asked(path, TOOL_GATE_SEEN) >= 1:
        return ''

    said = <своя проверка>

    if not said:
        claude_gate_forget(path, TOOL_GATE_SEEN)      # придираться не к чему

        return ''

    claude_gate_remember(path, TOOL_GATE_SEEN)

    return said

Ключ — то, что застава считает одним эпизодом: у одной это путь файла, у другой id
сессии. Бюджет придирок она тоже выбирает сама: раз на файл, две на эпизод.
"""
import json
import os
import time
from pathlib import Path

# Сколько живёт память о придирке. ⚠ Сутки: за сессию застава не должна мешать дважды,
# а через день сказать стоит снова — материал с тех пор изменился.
CLAUDE_GATE_SEEN_FOR = 24 * 60 * 60


def claude_gate_off(name) -> bool:
    """Снята ли застава переменной окружения — спускной клапан.

    Args:
        name: имя переменной, например `TOOL_GATE_PASS`.

    ⚠ Имя переменной каждая застава называет **своё и длинное**: короткое однажды
    окажется выставленным в общем окружении, и застава замолчит навсегда, а заметить
    это нечем — молчащая застава выглядит как отсутствующая.
    """
    return bool(os.environ.get(str(name)))


def claude_gate_asked(key, seen_path, now=None) -> int:
    """Сколько раз застава уже придиралась по этому эпизоду за сутки.

    Args:
        key: что застава считает одним эпизодом — путь файла, id сессии.
        seen_path: файл памяти; у каждой заставы свой.
        now: отметка времени вместо текущей — для тестов.

    Returns:
        int: ноль, если не придиралась или придирка протухла.
    """
    record = _load(seen_path, now=now).get(str(key)) or {}

    return int(record.get('asks') or 0)


def claude_gate_remember(key, seen_path, now=None) -> None:
    """Запомнить, что застава придралась по этому эпизоду.

    Args:
        key: эпизод — тот же, что у `claude_gate_asked`.
        seen_path: файл памяти; у каждой заставы свой.
        now: отметка времени вместо текущей — для тестов.

    ⚠⚠ Ошибка записи проглатывается: не суметь запомнить — значит придраться лишний
    раз, а упасть здесь значит отменить чужую работу без объяснения. Первое —
    неудобство, второе — поломка.
    """
    moment = float(time.time() if now is None else now)
    seen = _load(seen_path, now=now)
    record = seen.get(str(key)) or {}
    record['asks'] = int(record.get('asks') or 0) + 1
    record['at'] = moment
    seen[str(key)] = record

    _dump(seen_path, seen)


def claude_gate_forget(key, seen_path) -> None:
    """Забыть эпизод: придираться стало не к чему, счётчик сброшен.

    Args:
        key: эпизод — тот же, что у `claude_gate_asked`.
        seen_path: файл памяти; у каждой заставы свой.

    ⚠ Зовётся, когда **проверка ничего не нашла**, а не когда застава промолчала по
    бюджету: иначе исчерпанный бюджет сбрасывал бы сам себя и застава придиралась бы
    без конца.
    """
    seen = _load(seen_path)

    if seen.pop(str(key), None) is not None:
        _dump(seen_path, seen)


def _load(seen_path, now=None) -> dict:
    """Память о придирках: `{эпизод: {'asks': N, 'at': epoch}}`, протухшее выброшено.

    ⚠ Нет файла, битый файл, чужая форма — пустая память, а не исключение: застава
    стоит на каждом ходе, и упади она на своей памяти — работа встанет целиком.
    """
    moment = float(time.time() if now is None else now)

    try:
        got = json.loads(Path(seen_path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(got, dict):
        return {}

    fresh = {}

    for key, value in got.items():
        try:
            if isinstance(value, dict) and (moment - float(value.get('at') or 0)) < CLAUDE_GATE_SEEN_FOR:
                fresh[key] = value
        except (TypeError, ValueError):
            continue

    return fresh


def _dump(seen_path, seen) -> None:
    """Записать память. ⚠ Ошибка проглатывается — см. ⚠⚠ в `claude_gate_remember`."""
    spot = Path(seen_path)

    try:
        spot.parent.mkdir(parents=True, exist_ok=True)
        spot.write_text(json.dumps(seen), encoding='utf-8')
    except OSError:
        pass

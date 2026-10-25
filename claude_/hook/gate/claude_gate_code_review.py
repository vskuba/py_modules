#!/usr/bin/env python3
"""Застава ревизии после правки: код нарезан по трём файлам — сверь его с правилами.

Правила, которые она сторожит, — `py_modules/docs/code_rules.md`: один и тот же
функционал не живёт в двух файлах, DDD / SOLID / GRASP и имена по правилу префикса,
латиница в именах, приватные ниже публичных, мёртвый код удалён, документация
актуальна. Пока правка лежит в одном файле, её видно глазами вместе с правкой; на трёх
файлах изменение перестаёт читаться как целое — и ровно на этом размере «гляну потом»
не наступает никогда.

## ⚠⚠ Это `PostToolUse`: правку не отменяет, и не может

Точка стоит **после** того, как запись состоялась (`PreToolUse` на эти же инструменты
уже занят `claude_gate_tool_exists_check`). Отсюда два правила, которых нет у застав
отмены:

* замечание приходит агенту как ответ на **успешный** вызов, поэтому в тексте прямо
  сказано: правку не отменять — её надо проверить;
* зовётся застава на **каждую** правку, то есть десятки раз за сессию. Порог и память
  устроены так, чтобы из десятка вызовов голос прозвучал один раз на партию правок.

Вешается в `~/.claude/settings.json`:

    "PostToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit",
      "hooks": [{"type": "command",
        "command": "/usr/bin/python3 /home/vasyl/PycharmProjects/py_modules/claude_/hook/gate/claude_gate_code_review.py",
        "timeout": 60}]}]

## Партия = три файла, о которых ещё не говорили

Счёт идёт по **файлам, а не по нажатиям**: десять правок в одном файле — это одна
правка глазами, три файла — уже изменение. Застава собирает множество файлов кода,
тронутых от начала транскрипта, плюс файл текущего вызова (до строк транскрипта он ещё
не доехал), и вычитает из него те, о которых уже говорила. Не хватает трёх — молчит;
набралось — задаёт вопрос и дописывает партию в память, чтобы следующий вызов спрашивал
о новых файлах, а не пережёвывал эту.

Ключ эпизода — id сессии. Без вычитания застава кричала бы на каждую правку после
третьей; с одним лишь счётчиком придирок молчала бы до конца суток после первого
вопроса.

## Что застава считает сама, а что решает агент

Пункты, у которых есть машинная формулировка, она считает **сама** — готовыми
`tool_typo_cyrillic` (§1.2) и `function_order` (§3) по файлам партии, и кладёт находки в
замечание: цена — один `ast` на файл, а без них агент тратит круг на то, что уже
посчитано.

Судейские пункты — размазан ли функционал, на месте ли предметная область, жив ли
оставленный код, не обещает ли док удалённого, — застава **не решает**: контекста правки
у неё нет, а ложный вердикт «размазано» стоит дороже молчания. Она перечисляет файлы и
проверки; смотрит тот, кто правил.

## ⚠⚠ Орфография названа командой, а не посчитана, и причин две

`dictionary_spell_check_ru` ловит опечатки в докстрингах и комментариях — ровно тот
слой, который правится этими же вызовами. Но считать его здесь нельзя:

1. **Он работает по корпусу, а не по файлу.** Опечатку от термина отличает замена,
   которая встречается в репозитории часто; на одном файле частот нет, и проверка
   слепнет — не ошибается, а молчит. То есть звать её «по файлам партии», как
   `tool_typo_cyrillic`, бессмысленно по устройству.
2. **Полный прогон стоит 5,7 с** против долей секунды у `ast`-проверок, а застава
   срабатывает на каждой партии правок.

Поэтому пункт стоит в одном ряду с «не размазан ли функционал» и «удалено ли мёртвое»:
команда названа, смотрит тот, кто правил.

## Как отойти

    CLAUDE_GATE_CODE_REVIEW_PASS=1                # один вызов, без правки конфига
"""
import sys
from pathlib import Path

# ⚠ Загрузка через `spec_from_file_location` (так делает сюита) не кладёт корень
# слоя в путь поиска сама, а запуск файла по пути — кладёт через sys.path[0].
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from claude_.hook.claude_hook import (claude_hook_is_tool, claude_hook_journal_path,      # noqa: E402
                                      claude_hook_run, claude_hook_tool_calls,
                                      claude_hook_transcript_rows)
from claude_.hook.gate.claude_gate import (claude_gate_asked,                            # noqa: E402
                                          claude_gate_forget, claude_gate_note,
                                          claude_gate_noted, claude_gate_off,
                                          claude_gate_spot)
from function_.function_order import function_order, function_order_format               # noqa: E402
from tool_.tool_typo import tool_typo_cyrillic                                            # noqa: E402

# Память: `{сессия: {'asks': N, 'seen': [файлы партии], 'at': epoch}}`, сутки (`claude_gate.md`).
CLAUDE_GATE_CODE_REVIEW_SEEN = Path.home() / '.cache' / 'claude_gate_code_review_seen.json'
CLAUDE_GATE_CODE_REVIEW_MIN_FILES = 3                  # файлов кода в партии, с которых говорим
CLAUDE_GATE_CODE_REVIEW_ASKS = 3                       # вопросов на сутки, дальше — тишина
CLAUDE_GATE_CODE_REVIEW_ROWS = 800                     # записей транскрипта на разбор
CLAUDE_GATE_CODE_REVIEW_SHOWN = 6                      # файлов в замечании, дальше «и ещё N»
CLAUDE_GATE_CODE_REVIEW_FOUND = 5                      # механических находок на проверку
CLAUDE_GATE_CODE_REVIEW_PASS = 'CLAUDE_GATE_CODE_REVIEW_PASS'
CLAUDE_GATE_CODE_REVIEW_RULES = 'py_modules/docs/code_rules.md'
# Инструменты правки — они же матчер в `settings.json` и отсечка в обвязке хука.
CLAUDE_GATE_CODE_REVIEW_TOOLS = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit')
# Что считаем кодом, а не прозой и не вёрсткой. ⚠ `.md`, `.json`, `.html` здесь нет
# намеренно: правка текста и правка страницы — не правка функционала, у них свои заставы
# (`claude_gate_tool_exists_check` сторожит прозу, `claude_gate_design_review_live` — вёрстку).
CLAUDE_GATE_CODE_REVIEW_SUFFIXES = ('.py', '.pyi', '.ipynb', '.js', '.mjs', '.cjs', '.jsx',
                                    '.tsx', '.ts', '.vue', '.svelte', '.php', '.sql', '.sh')
# Чужое и собранное: туда правка агента руками не заходит, и ревизии там не для кого.
CLAUDE_GATE_CODE_REVIEW_SKIP = ('node_modules', '.git', 'dist', 'build', '.next', 'vendor',
                                '.venv', 'venv', '__pycache__', '.mypy_cache')


def claude_gate_code_review(path, transcript_path, session_id, seen_path=None,
                            now=None) -> dict:
    """Снять вердикт с этой правки: партия из трёх файлов ревизии её ещё не видела.

    Args:
        path: файл текущего вызова — из `tool_input`; его в транскрипте ещё нет.
        transcript_path: `.jsonl` сессии из ввода хука — всё, что правили до этого.
        session_id: id сессии; по нему живут счётчик придирок и состав партий.
        seen_path: где лежит память; пусто — `CLAUDE_GATE_CODE_REVIEW_SEEN`.
        now: отметка времени вместо текущей — для тестов.

    ⚠ Путь по умолчанию берётся **в момент вызова**, а не в момент объявления:
    привязанный аргументом файл нельзя подменить в тесте, и проверка натыкалась бы
    на живую память хозяина.

    Returns:
        dict: {'block': bool, 'found': [файлы партии], 'lint': {…}, 'why': str}.
            `found` — файлы, о которых ещё не говорили; `lint` — механические находки
            по ним, и только когда держим вопрос.

    ⚠⚠ Память пишется **здесь же**, где и решение держать: не доедь она до диска,
    каждый следующий вызов задал бы тот же вопрос заново — а это крик на каждую
    правку до конца суток.
    """
    seen = claude_gate_spot(seen_path or CLAUDE_GATE_CODE_REVIEW_SEEN)

    if claude_gate_off(CLAUDE_GATE_CODE_REVIEW_PASS):
        return {'block': False, 'found': [], 'lint': {},
                'why': f'снята через {CLAUDE_GATE_CODE_REVIEW_PASS}'}

    touched = claude_gate_code_review_edits(transcript_path)

    # Текущая правка в транскрипте ещё не обязана быть — дописываем её сами.
    if _is_code(path) and path not in touched:
        touched.append(path)

    if not touched:
        # Эпизода нет: кода в этой сессии не трогали. Появится — застава вспомнит заново.
        claude_gate_forget(session_id, seen)

        return {'block': False, 'found': [], 'lint': {}, 'why': 'правок кода нет'}

    covered = {str(one) for one in (claude_gate_noted(session_id, seen).get('seen') or [])}
    pending = [one for one in touched if one not in covered]

    if len(pending) < CLAUDE_GATE_CODE_REVIEW_MIN_FILES:
        return {'block': False, 'found': pending, 'lint': {},
                'why': f'новых файлов {len(pending)}, порог '
                       f'{CLAUDE_GATE_CODE_REVIEW_MIN_FILES}'}

    asked = claude_gate_asked(session_id, seen, now=now)

    if asked >= CLAUDE_GATE_CODE_REVIEW_ASKS:
        return {'block': False, 'found': pending, 'lint': {},
                'why': f'вопросов {asked} из {CLAUDE_GATE_CODE_REVIEW_ASKS} — '
                       'бюджет исчерпан'}

    claude_gate_note(session_id, seen,
                     {'asks': asked + 1, 'seen': sorted(covered | set(pending))}, now=now)

    if claude_gate_asked(session_id, seen, now=now) <= asked:
        # ⚠ Память не записалась — каталог оказался закрыт на запись, и `spot` ему не
        # помог. Без счётчика этот вопрос повторялся бы на каждую правку до конца
        # суток; молчание здесь честнее крика, а заметить его можно по `why`.
        return {'block': False, 'found': pending, 'lint': {},
                'why': 'память не записалась — застава себя снимает'}

    return {'block': True, 'found': pending, 'lint': claude_gate_code_review_lint(pending),
            'why': f'файлов кода в партии: {len(pending)}'}


def claude_gate_code_review_edits(transcript_path, rows=None) -> list:
    """Файлы кода, правленные в транскрипте, — по одному на файл, по порядку правок.

    Args:
        transcript_path: `.jsonl` сессии.
        rows: готовые записи вместо чтения файла; для тестов.

    Returns:
        list: пути файлов кода в порядке первой правки. Не-код (проза, вёрстка) и
            служебные каталоги сюда не попадают вовсе — их судит не эта застава.

    ⚠ Порог считается по **файлам**, а не по правкам: десять нажатий в одном файле
    остаются одной правкой глазами, три файла — уже изменение, которое надо видеть
    целиком.
    """
    rows = claude_hook_transcript_rows(transcript_path, CLAUDE_GATE_CODE_REVIEW_ROWS) if rows is None else rows
    out = []

    for call in claude_hook_tool_calls(rows):
        if not claude_hook_is_tool(call['name'], CLAUDE_GATE_CODE_REVIEW_TOOLS):
            continue

        tool_input = call['input'] or {}
        where = str(tool_input.get('file_path') or tool_input.get('notebook_path') or '')

        if where and _is_code(where) and where not in out:
            out.append(where)

    return out


def claude_gate_code_review_lint(paths) -> dict:
    """Машинная половина проверок — по файлам партии, без суда над смыслом.

    Args:
        paths: пути файлов партии; не-`.py` и нечитаемые пропускаются.

    Returns:
        dict: {'cyrillic': находки `tool_typo_cyrillic`, 'order': находки
            `function_order`}. Оба поля пусты — механика чистая. К находкам
            подставлено `file` — путь того файла, откуда они. Судейские пункты
            (размазан ли функционал, жив ли код, актуальны ли доки) здесь **не**
            считаются: у заставы нет контекста правки.

    ⚠ Кириллицу судит `tool_typo_cyrillic`, а не `variable_cyrillic`: первому видно
    **само имя** функции или ключ словаря, а второй смотрит имена внутри тела — то
    есть файл с `def ai_лицо()` и латиничным телом прошёл бы мимо. Разбираются только
    `.py` — `ast`; для `.js`, `.php` и прочих остаются grep-шаблоны §1.2.

    ⚠ Сбой на одном файле не отменяет находок с остальных: этот файл молча
    пропускается. Застава и так обязана отпускать вызов при своей поломке
    (`claude_hook.md`), но до обвязки лучше не доводить: там молчит уже весь список.
    """
    cyr, order = [], []

    for spot in paths or []:
        if not str(spot).lower().endswith('.py'):
            continue

        try:
            if not Path(spot).is_file():
                continue

            # Поле `where` у находок — путь относительно корня, а корнем здесь сам
            # файл: в замечании от него осталось бы голое имя без каталога.
            cyr.extend(dict(one, file=str(spot)) for one in tool_typo_cyrillic(Path(spot)))
            order.extend(function_order(str(spot)))
        except Exception:      # noqa: BLE001 — файл молча пропускаем, см. ⚠ выше
            continue

    return {'cyrillic': cyr, 'order': order}


def claude_gate_code_review_format(verdict) -> str:
    """Собрать замечание, которое агент прочитает вместо продолжения работы."""
    if not verdict.get('block'):
        return ''

    found = verdict['found']
    files = [f'  {one}' for one in found[:CLAUDE_GATE_CODE_REVIEW_SHOWN]]

    if len(found) > CLAUDE_GATE_CODE_REVIEW_SHOWN:
        files.append(f'  … и ещё {len(found) - CLAUDE_GATE_CODE_REVIEW_SHOWN}')

    lint = verdict.get('lint') or {}

    return '\n'.join((
        f'ЗАСТАВА (ревизия кода): правкой затронуто {len(found)} файла кода — один файл',
        'виден вместе с правкой, три уже не читаются как целое',
        f'({CLAUDE_GATE_CODE_REVIEW_RULES}). Правку не отменять — она удалась,',
        'её надо посмотреть:',
        *files,
        '',
        'Что проверить по затронутому функционалу (границы — эта правка и то, что её',
        'читает, а не весь репозиторий):',
        '1. Не размазан ли один и тот же функционал по файлам: за одну логику отвечает',
        '   один паттерн, второе похожее место — уже точка различия в нём, а не вторая',
        '   функция (§4.2, §4.4). `python -m tool_.tool_impact "<имя>"` — кто ещё это',
        '   делает; `python -m tool_.tool_find "<что делаете>"` — нет ли этого в общем слое.',
        '2. DDD / SOLID / GRASP: публичная функция названа префиксом своего файла (§1,',
        '   §1.1), файл стоит в своей предметной области (§0.1, §4.1), знание проекта',
        '   не протекло в общий слой (§5).',
        *_cyrillic_lines(lint),
        *_order_lines(lint),
        '5. Мёртвое, оставленное этой правкой, удалено: `python -m tool_.tool_import',
        '   <файл>` — непрочитанные импорты, `python -m tool_.tool_dead <каталог>',
        '   --private` — имена, которых никто не зовёт.',
        '6. Документация актуальна: новый функционал расписан детально, удалённый',
        '   больше не упоминается — строка на тронутый модуль в `docs/` или в',
        '   `.claude/docs/` проекта (`python -m commit_.commit_scope` покажет дыры).',
        '7. Опечаток в написанной прозе нет: `python -m',
        '   dictionary_.dictionary_spell_check_ru` — слово, незнакомое словарю, чья',
        '   замена в одну букву частотна в репозитории. ⚠ Корпусу нужен весь корень,',
        '   по одному файлу проверка слепнет; своё слово ремесла — в `.spell-allow`.',
        '',
        'Не продолжаем, пока список не пройден: назови вердикт по каждому пункту — или',
        'вслух скажи, почему пункт здесь не при чём. Вопрос один на партию правок;',
        f'снятие: {CLAUDE_GATE_CODE_REVIEW_PASS}=1.',
    ))


def main() -> int:
    """Хук `PostToolUse` на инструменты правки: код 2 отдаёт замечание агенту, 0 — молчит.

    ⚠ Разбор ввода, коды возврата и «при своей поломке пропускать» — в `claude_hook`;
    клапан, бюджет и память — в `claude_gate`. Здесь только вопрос: набралась ли партия
    правок на ревизию и что в ней уже видно механике.
    """
    return claude_hook_run(CLAUDE_GATE_CODE_REVIEW_TOOLS, _look)


def _look(data) -> str:
    """Замечание на эту правку либо пусто. Вид проверки для `claude_hook_run`."""
    transcript = claude_hook_journal_path(data)         # путь DSH: в его вводе его нет
    session = str(data.get('session_id') or '')
    tool_input = data.get('tool_input') or {}
    where = str(tool_input.get('file_path') or tool_input.get('notebook_path') or '')

    if not transcript or not session or not where:
        return ''

    verdict = claude_gate_code_review(where, transcript, session)

    return claude_gate_code_review_format(verdict) if verdict['block'] else ''


def _is_code(where) -> bool:
    """Код ли это — не проза, не вёрстка и не чужое собранное.

    ⚠ Служебные каталоги сверяются по **именам в пути**, а не по префиксу: правка
    лежит в репозитории, а репозиторий может стоять где угодно.
    """
    spot = str(where)

    if not spot.lower().endswith(CLAUDE_GATE_CODE_REVIEW_SUFFIXES):
        return False

    return not any(part in CLAUDE_GATE_CODE_REVIEW_SKIP for part in Path(spot).parts[:-1])


def _cyrillic_lines(lint) -> list:
    """Пункт 3 замечания: находки кириллицы в именах или «проверено, чисто»."""
    found = lint.get('cyrillic') or []

    if not found:
        return ['3. Кириллица в именах и ключах (§1.2): по файлам партии проверено — чисто;',
                '   в остальном репозитории и в не-`.py`: `python -m tool_.tool_typo --cyrillic`.']

    lines = ['3. Кириллица в именах и ключах (§1.2) — застава нашла сама:',
             *[f"   {one.get('file', '')}:{one.get('line', 0)} {one.get('word', '')}"
               f" — {one.get('hint', '')}" for one in found[:CLAUDE_GATE_CODE_REVIEW_FOUND]]]

    if len(found) > CLAUDE_GATE_CODE_REVIEW_FOUND:
        lines.append(f'   … и ещё {len(found) - CLAUDE_GATE_CODE_REVIEW_FOUND}')

    return lines


def _order_lines(lint) -> list:
    """Пункт 4 замечания: порядок определений в файлах партии, отчёт `function_order`."""
    found = lint.get('order') or []

    if not found:
        return ['4. Порядок определений в файле (§3): по файлам партии проверено —',
                '   приватные ниже публичных, `__main__` последний.']

    return ['4. Порядок определений в файле (§3) — застава нашла сама:',
            # Пустые строки отчёта режем: здесь они превращаются в строку из пробелов.
            *[f'   {one.strip()}' for one in function_order_format(found).splitlines() if one.strip()]]


if __name__ == '__main__':
    sys.exit(main())

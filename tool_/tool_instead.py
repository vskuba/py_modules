"""
Что из общего слоя делает то же, что вот этот код: проверка **по факту**, а не по намерению.

`tool_find` спрашивают словами, и потому он помогает лишь тому, кто **собрался**
искать. А своё пишут не по решению, а ползком: сначала одна команда `bash`, потом
она же с трубой, потом heredoc на тридцать строк, потом файл — и к мигу, когда
видно, что это инструмент, он уже написан.

Этот вход другой: ему дают **готовый текст** — файл, кусок скрипта, команду — и он
отвечает, что из слоя уже это умеет. Намерения не требуется вовсе.

    PYTHONPATH=py_modules python -m tool_.tool_instead scratchpad/shot.py
    echo "$команда" | PYTHONPATH=py_modules python -m tool_.tool_instead

## ⚠⚠ Ищет по приметам ремесла, а не по словам задачи

Слова задачи в скрипте не написаны: там `docker exec … mysqldump`, `playwright`,
`ssh … bash -s`. Поэтому поиск идёт от **примет** — команд, модулей, вызовов, —
и каждая примета знает, какими словами её тема лежит в описи.

Ровно так были бы пойманы случаи, где своё писалось поверх готового: репетиция
миграции (`mysql_rehearse`), `ssh` с heredoc (`ssh_x`), снимок страницы браузером
(`web_drive_page`), поиск кириллицы в именах (`variable_cyrillic`).

## ⚠ Слабое место — список примет

Он рукописный и потому неполон: чего в нём нет, того инструмент не увидит. Это
осознанный размен. Угадывать «о чём этот код» без списка можно только моделью, а
проверка обязана быть мгновенной и работать без сети — иначе её перестанут звать,
и она повторит судьбу `tool_find`.

⚠ Найденное — **кандидаты, а не приговор**. Совпала примета `docker exec` — это
может быть и запуск тестов, и дамп; подходит ли, смотреть всё равно человеку.

⚠⚠ Пусто в ответе **не значит «можно писать своё»**: значит лишь, что примета не
описана. Список примет и пополняют тогда, когда `tool_find` словами что-то нашёл,
а эта проверка промолчала.
"""
import re
import sys

from pathlib import Path

# Файл запускают и путём (`python3 py_modules/tool_/tool_instead.py`). Тогда первым в
# путях лежит каталог файла, и `import tool_` находит соседний модуль вместо пакета.
if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_.tool_catalog import tool_catalog
# ⚠ Приватные помощники соседа взяты намеренно и осознанно. Своё сведение слов здесь
# уже было — подстрочное, — и оно роняло главный пример из докстринга: «миграции» не
# подстрока «миграцию», и `mysql_rehearse_run` не находился вовсе. Морфология живёт в
# `tool_find` (`_kinship`), и второй её вариант — ровно то, против чего этот модуль.
#
# ⚠⚠ Сам `tool_find` целиком здесь не позовёшь: он собирает опись каждым вызовом
# (полторы секунды), а примет полтора десятка. Отсюда — его сведение, но своя опись.
from tool_.tool_find import _kinship, _tokens

# Сколько кандидатов показываем на примету. ⚠ Мало намеренно: длинный список
# читают по диагонали, а смысл проверки — увидеть, что готовое **есть**.
TOOL_INSTEAD_LIMIT = 3

# Слова короче этого в приметах не ищутся: «ssh» и «git» — предметы, а «на», «из» —
# шум, который совпадает с каждой второй строкой описи.
TOOL_INSTEAD_MIN_WORD = 3

# Приметы ремесла: что встретилось в коде → где и какими словами это лежит в описи.
#
# ⚠⚠ Три поля: регулярное выражение **по тексту кода**, namespace'ы, внутри которых
# ищем, и слова для сверки с описью. Имена функций прямо не пишем: переименуют — и
# примета протухнет молча, а namespace со словами темы переименование переживают.
#
# ⚠⚠ Namespace здесь — не украшение, а то, что делает выдачу читаемой. Без него
# слово «таблиц» из приметы дампа ловило `adb_burst_timeline` («разложить серию
# кадров в таблицу метрик»), а «пароль» — `auth_password_hash`: совпадение честное,
# смысл никакой. Пусто вместо namespace значит «искать по всему слою».
#
# ⚠ Порядок важен: узкая примета идёт раньше общей. Иначе `docker exec` перехватывал
# бы и дамп, и запрос, и прогон сюиты.
#
# ⚠⚠ Слова приметы пишутся **словарём самого слоя** — теми, что стоят в первой строке
# докстринга нужной функции, — а не тем, как назвал бы тему автор приметы. Сведение
# лексическое: «репетиция миграции» не находило `mysql_rehearse_run` вовсе, потому что
# у него написано «прогнать миграцию на свежей копии дважды», а слово «репетиция»
# живёт в докстринге **модуля**, в опись функций не попадающем.
#
# ⚠ Добавив примету, проверьте её: `python -m tool_.tool_instead --marks` печатает
# лучшего кандидата каждой. Примета, ведущая не туда, хуже отсутствующей — она
# отвечает уверенно и неверно.
TOOL_INSTEAD_MARKS = (
    (r'mysqldump[^|]*\|\s*gzip|mysqldump[^|]*>\s*\S+\.sql',
     ('mysql_/', 'sql_/'), 'дамп выгрузка таблиц'),
    (r'DROP\s+DATABASE|rehears',
     ('mysql_/',), 'прогнать миграцию копии дважды'),
    (r'_yoyo_migration|\byoyo\b',
     ('mysql_/',), 'накатить миграции yoyo'),
    # ⚠ Третий вид записи — `ssh` элементом списка доводов (`subprocess.run(['ssh',
    # host, …])`): без него примета видела только командную строку, а из кода `ssh`
    # чаще зовут именно списком.
    (r'ssh\s+\S+\s+[\'"]?bash\s+-s|ssh\s+\S+\s*<<|\bscp\s|[\'"]ssh[\'"]\s*,',
     ('ssh_/',), 'скрипт на удалённом хосте контейнере'),
    (r'docker\s+exec[^\n]*\bmysql\b',
     ('mysql_/',), 'выполнить запрос результат'),
    (r'playwright|async_playwright|chromium\.launch|page\.goto',
     ('web_/', 'browser_/'), 'страница скрипт снимок вход'),
    (r'points/(scroll|delete|search)|:6333',
     ('qdrant_/',), 'поиск коллекция сохранение'),
    (r'\bcurl\b[^\n]*(-X\s+POST|Content-Type)|httpx\.AsyncClient\(',
     ('http_/',), 'общий транспорт соединения переиспользует'),
    # ⚠⚠ Не просто `pytest`: так примета срабатывала на **каждом** файле сюиты —
    # тест и обязан его импортировать. Речь о запуске прогона из кода и о разборе
    # чужого кода в приёмке, а не о самом факте тестов.
    (r'subprocess[^\n]*pytest|\bpytest\b\s+-n\s',
     ('pytest_/',), 'сюита прогон сторож'),
    (r'ast\.parse|ast\.walk',
     ('function_/', 'tool_/', 'variable_/'), 'нарушения порядка файле дереве'),
    (r'cyrillic|def\s+\w*[а-яё]|^\s*[а-яё]\w*\s*=\s*[^=]',
     ('variable_/', 'tool_/'), 'кириллические имена коде'),
    # ⚠ `json.loads` вообще — не примета: им читают настройки, тела запросов, что
    # угодно. Примета — разбор **ответа модели**, где на чистый JSON рассчитывать
    # нельзя; отсюда слова рядом в той же строке.
    (r'json\.loads?\([^\n]*(answer|content|reply|response|llm|ответ)',
     ('json_/',), 'вынуть JSON-объект ответа'),
    (r'\bgit\s+(commit|push|status|diff)\b',
     ('commit_/', 'deploy_/'), 'изменённое подсистемам стаженные'),
    (r'\\u[0-9a-fA-F]{4}|surrogate|emoji',
     ('text_/',), 'эмодзи текст'),
    (r'INSERT\s+INTO[^\n]*SELECT|ON\s+DUPLICATE\s+KEY',
     ('mysql_/',), 'строки перенести базами'),
)


def tool_instead(code, root=None) -> list[dict]:
    """Чем это уже делают в общем слое: проверка готового кода на дублирование.

    Args:
        code: текст скрипта, команды или файла — как есть.
        root: корень py_modules; пусто — тот, в котором лежит этот файл.

    Returns:
        list[dict]: записи `{mark, line, tools}` — слова сработавшей приметы,
        первая строка кода, где она встретилась, и кандидаты из описи (`where`,
        `name`, `summary`). Пусто — примета не описана, и это законный исход.

    ⚠ Одна примета на строку не гарантируется, а вот одна находка на примету — да:
    показывается **первое** место, где она встретилась. Полный список совпадений
    отчёт бы утопил, а нужно одно — понять, что готовое есть.
    """
    text = str(code or '')
    if not text.strip():
        return []

    catalog = tool_catalog(root)
    found = []

    for pattern, where, words in TOOL_INSTEAD_MARKS:
        place = _first_line(text, pattern)
        if place is None:
            continue

        picked = _pick(catalog, where, words)
        if not picked:
            continue

        found.append({'mark': words, 'line': place, 'tools': picked})

    return found


def tool_instead_format(found) -> str:
    """Отчёт словами. Пусто — примета не описана, а не «готового нет»."""
    if not found:
        return ('Примет не нашлось. ⚠ Это не значит «готового нет» — значит, приметы\n'
                'нет в списке. Спросите словами: '
                'python -m tool_.tool_find "<что делаете>"')

    out = ['Похоже, это уже умеют:']

    for one in found:
        out.append(f"\n  {one['line']}")
        for tool in one['tools']:
            out.append(f"    → {tool['where']}:{tool['name']}")
            out.append(f"       {tool['summary']}")

    out.append('\n⚠ Это кандидаты, а не приговор: подходит ли — смотреть вам.')

    return '\n'.join(out)


def tool_instead_marks(root=None) -> list[dict]:
    """Куда ведёт каждая примета: её лучший кандидат.

    Returns:
        list[dict]: `{mark, tool}` по записи на примету; `tool` пуст — примета не
        находит ничего, и это поломка, а не особенность.

    ⚠ Это проверка самого списка примет, а не кода. Зовётся при правке списка:
    слова пишутся словарём слоя, и промах виден только так.
    """
    catalog = tool_catalog(root)
    out = []

    for _, where, words in TOOL_INSTEAD_MARKS:
        top = _pick(catalog, where, words)
        out.append({'mark': words,
                    'tool': f"{top[0]['where']}:{top[0]['name']}" if top else ''})

    return out


def main() -> int:
    """CLI: `tool_instead <файл>`, текст через stdin, либо `--marks` — куда ведут приметы."""
    if len(sys.argv) > 1 and sys.argv[1] == '--marks':
        for one in tool_instead_marks():
            print(f"{one['mark'][:44]:46} → {one['tool'] or '⚠ НИЧЕГО'}")

        return 0

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if not path.is_file():
            print(f'Нет файла: {path}')

            return 1
        code = path.read_text(encoding='utf-8', errors='replace')
    else:
        code = sys.stdin.read()

    print(tool_instead_format(tool_instead(code)))

    return 0


def _first_line(text, pattern):
    """Первая строка кода, где встретилась примета. Обрезана до читаемой длины."""
    look = re.compile(pattern, re.IGNORECASE)

    for line in text.splitlines():
        if look.search(line):
            ready = line.strip()

            return ready[:100] + ('…' if len(ready) > 100 else '')

    return None


def _pick(catalog, where, words) -> list[dict]:
    """Кандидаты приметы: сперва отбор по namespace, потом счёт совпавших слов.

    ⚠ Свой простой подсчёт, а не вызов `tool_find`: тот отдаёт текст для человека и
    себя из выдачи убирает, а здесь нужны записи. Сведение по общему куску слова
    здесь не нужно — слова приметы пишутся сразу в той форме, что лежит в описи.

    ⚠⚠ Namespace отбирает **до** счёта, и ни одно слово не выводит за его границы:
    примета знает, где лежит её тема, а слова лишь расставляют внутри. Пустой
    `where` снимает границу — тогда работает один счёт слов, со всем его шумом.
    """
    need = {one for one in _tokens(words) if len(one) >= TOOL_INSTEAD_MIN_WORD}
    score = []

    for row in catalog:
        if row.get('kind') != 'func':
            continue

        place = row.get('where', '')
        if where and not any(place.startswith(one) for one in where):
            continue

        stack = set(_tokens(f"{row.get('name', '')} {row.get('summary', '')} {place}"))
        weight = sum(max((_kinship(one, token) for token in stack), default=0.0)
                     for one in need)

        if weight:
            score.append((weight, row))

    # ⚠ При равном весе порядок задаёт путь, а не случай: иначе одна и та же примета
    # печатала бы разных кандидатов от запуска к запуску, и проверке нельзя верить.
    score.sort(key=lambda pair: (-pair[0], pair[1]['where'], pair[1]['name']))

    return [{'where': row['where'], 'name': row['name'],
             'summary': row.get('summary', '')} for _, row in score[:TOOL_INSTEAD_LIMIT]]


if __name__ == '__main__':
    sys.exit(main())

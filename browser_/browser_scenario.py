"""Сценарий — последовательность действий над страницей и её проверка.

Здесь только схема сценария и подстановка переменных. Ни Playwright, ни MySQL:
файл читают обе стороны — тот, кто сценарий выполняет (`browser_scenario_run` в
контейнере), и тот, кто его хранит и проверяет на сохранении (веб-приложение с
базой). Две копии одной схемы разошлись бы на первой правке.

Сценарий детерминирован: ни один шаг не «ищет похожее». Селектор обязан совпасть
ровно с одним элементом (`page.locator` в строгом режиме), ожидание задано явно,
а таймаут — у каждого шага свой. Сценарий, который сработал случайно, хуже
упавшего: упавший заводит задачу на переписывание, случайный молча врёт.

**Пароль в сценарии не хранится.** Значения приходят на прогон: `variables`
попадают в журнал, `secrets` — нет, они замазываются в тексте ошибок
(`browser_scenario_mask`), а сам шаг журналируется до подстановки, то есть как
`{{password}}`.
"""

import re

from pydantic import BaseModel, Field, field_validator, model_validator

# Действие → поля, без которых оно бессмысленно. Список закрытый: чего здесь нет,
# того сценарий сделать не может, и автор сценария (человек в форме или LLM)
# увидит отказ схемы, а не молча пропущенный шаг.
BROWSER_SCENARIO_ACTIONS = {
    'goto':        ('url',),
    'click':       ('selector',),
    # Наведение отдельным действием, а не «кликом без нажатия»: подменю и всплывающие
    # подсказки открываются именно по `mouseover`, и клик по пункту, которого ещё нет
    # на экране, падает раньше, чем страница успевает его показать.
    'hover':       ('selector',),
    'fill':        ('selector', 'value'),
    'type':        ('selector', 'value'),
    'press':       ('key',),
    'select':      ('selector', 'value'),
    'wait_for':    ('selector',),
    'wait_url':    ('url',),
    'wait_ms':     ('value',),
    # Ожидание по признаку, а не по часам. `wait_ms` — это «подожди на всякий
    # случай»: на быстрой странице он тратит время впустую, на медленной не
    # дожидается. Эти два ждут ровно того, чего ждут.
    'wait_stable': (),              # value — сколько мс покоя DOM считать концом отрисовки
    'wait_text':   ('value',),      # selector необязателен: пусто — искать по всей странице
    'scroll':      ('value',),
    'extract':     ('selector', 'name'),
    # Построчный сбор: `selector` — строка списка, `fields` — что взять внутри неё.
    # Отдельным действием, а не режимом `extract`, потому что возвращает другое:
    # `extract` даёт строку, этот — список записей.
    'extract_rows': ('selector', 'name', 'fields'),
    # Читаемый текст страницы под именем `name` — то, что видит человек, без
    # разметки. `extract` для этого не годится: он отвечает на вопрос «что в этом
    # элементе», а здесь вопрос «о чём вообще эта страница», и ответ на него нужен
    # модели, а не селектору.
    'read_text':   ('name',),
    'assert_text': ('selector', 'value'),
    # Файлы. Путь — внутри того контейнера, где живёт браузер: в поле формы
    # подставляется файл его файловой системы, и скачанное падает туда же.
    'upload':      ('selector', 'value'),   # value — путь; несколько — через запятую
    'download':    ('selector', 'name'),    # нажать и дождаться файла, имя и путь → в `name`
    # Перехват сети: чем страница разговаривает с сервером.
    #
    # ЗАЧЕМ. Сценарий умеет нажать кнопку, но не умеет ответить на вопрос «а куда
    # она постит». Между тем именно это и нужно, когда сценарий хотят **заменить**
    # HTTP-запросом: страница чата, отправка сообщения, добавление в избранное —
    # все они дороги (десятки секунд живого браузера), и все сводятся к одному
    # запросу, если этот запрос знать.
    #
    # Живой случай: отправка реплики в чат стороннего сайта сценарием стоила 60
    # секунд на сообщение (замер прогона). Документации на API чата у сайта нет
    # вовсе, страница одностраничная, и найти адрес отправки было нечем: `extract`
    # ждёт элемент **видимым**, а `<script>` и `input[type=hidden]` невидимы
    # всегда. Отсюда эти два действия.
    #
    # ⚠ Их два, а не одно, и читается это как работа: «слежу → нажимаю → снимаю».
    # Одним действием пришлось бы либо писать всегда (лишние записи в журнале
    # каждого прогона), либо угадывать, когда остановиться.
    'net_watch': (),            # value — фильтр по URL; пусто — писать всё
    'net_dump': ('name',),      # name — куда сложить; value — фильтр поверх снятого
    # Журнал самой страницы — той же парой «слежу → снимаю». Отвечает на вопрос,
    # на который трасса шага ответить не может: шаг упал не потому, что селектор
    # плох, а потому, что на странице не отработал её собственный JavaScript.
    'console_watch': (),        # value — фильтр по тексту; пусто — писать всё
    'console_dump': ('name',),  # name — куда сложить; value — фильтр поверх снятого
}

# Что действие кладёт в `extract` и в какой форме. ⚠ **Один источник правды на два
# берега.** Форму ответа спрашивает не только прогон, но и тот, кто отдаёт
# клиентам список «что этот сценарий умеет вернуть»; сложенная там своя копия
# развилки `if action == …` отставала бы от этой таблицы на каждое новое действие —
# и новый сбор данных молча не появлялся бы в списке.
#
# `text` — строка (несколько совпадений склеены переносами), `rows` — список записей.
BROWSER_SCENARIO_RETURNS = {
    'extract': 'text',
    'extract_rows': 'rows',
    'read_text': 'text',
    'download': 'text',
    'net_dump': 'rows',
    'console_dump': 'rows',
}

# Сколько запросов держим за прогон. Страница чата шлёт их десятками (картинки,
# аналитика, опросы статуса), и без предела трасса прогона распухла бы до
# бесполезной. Двести — с запасом на любой разбор.
BROWSER_SCENARIO_NET_LIMIT = 200

# Сколько знаков тела запроса берём. Тело нужно, чтобы повторить запрос своими
# силами; целиком оно бывает мегабайтом (загрузка файла), и в журнал такому не
# место.
BROWSER_SCENARIO_NET_BODY_LIMIT = 4000

# Какие заголовки записываем. ⚠ **Список закрытый, и это про безопасность, а не
# про объём.** В заголовках живут `cookie` и `authorization` — сама сессия; уедь
# они в `extract`, чужая сессия оказалась бы в нашей базе и в ответе API открытым
# текстом. Здесь только то, что нужно, чтобы повторить запрос: как закодировано
# тело и чем страница себя объявляет.
BROWSER_SCENARIO_NET_HEADERS = ('content-type', 'x-requested-with', 'accept',
                                'origin', 'referer')

# 10 с на шаг: столько ждёт Playwright появления элемента, прежде чем признать,
# что вёрстка изменилась. Больше — и упавший сценарий висит минутами вместо того,
# чтобы быстро завести задачу на починку.
BROWSER_SCENARIO_STEP_TIMEOUT_MS = 10000

BROWSER_SCENARIO_STEP_LIMIT = 100

# Имена переменных — только буквы, цифры и подчёркивание: подстановка идёт в
# селекторы и URL, где фигурные скобки встречаются и сами по себе (CSS, JSON).
BROWSER_SCENARIO_VAR_RE = re.compile(r'\{\{\s*([A-Za-z0-9_]+)\s*\}\}')

# `value: "@src"` у `extract` — снять атрибут, а не текст. Иначе адрес картинки
# не достать: у `<img>` текста нет, и шаг вернул бы пустую строку.
BROWSER_SCENARIO_ATTR_MARK = '@'

# Имя `value` — особое: у поля формы читаем не атрибут, а текущее значение.
# `get_attribute('value')` отдаёт то, что стояло в разметке при загрузке, и у полей,
# заполненных скриптом, возвращает пусто. Живьём так ведёт себя любая карточка,
# которую рисует JavaScript: в дереве доступности значение видно (`textbox: Diana`),
# а атрибута нет вовсе — сбор молча отдавал пустые строки. Текста у `<input>` тоже
# нет, так что без этой ветки значения полей формы не достать ничем.
BROWSER_SCENARIO_ATTR_VALUE = 'value'

# Предел вложенности полей у `extract_rows`. Вложенность нужна для списка внутри
# записи — фотографии карточки, её метки, — и двух уровней на это хватает.
# Ограничение не от жадности: каждый уровень умножает число запросов к странице на
# число найденных строк, и дерево вглубь превращает один шаг в тысячи обращений.
BROWSER_SCENARIO_FIELDS_DEPTH = 3

# Сколько строк берём из одного `extract_rows`. Список на сайте бывает бесконечным
# (лента с подгрузкой), и без предела шаг утащил бы всю ленту в один ответ.
BROWSER_SCENARIO_ROWS_LIMIT = 500


class BrowserScenarioField(BaseModel):
    """
    Одно поле записи у `extract_rows`: что взять внутри строки списка.

    Селектор здесь **относительный** — ищется внутри своей строки, а не по всей
    странице. В этом вся суть построчного сбора: сшивка полей получается
    структурной, а не позиционной. Отдельными шагами `extract` она позиционная —
    имена и города приходят двумя списками, которые клиент сшивает по номеру, и
    пустое значение в одном из них сдвигает всё, что после, без единой ошибки.

    `attribute` — снять атрибут вместо текста (`href`, `src`). У `extract` для этого
    служит `@`-метка в значении; здесь поле отдельное, потому что описание поля и так
    структура, и прятать смысл в префиксе строки незачем.

    `mandatory` — строка без этого поля записью не считается и в ответ не попадает.
    Нужно, чтобы отсеивать то, что подошло под селектор строки по совпадению:
    заголовок таблицы, разделитель, рекламный блок. Без такого признака они
    приезжали бы записями с пустыми полями, и отличить их от настоящих было бы
    нельзя.

    `fields` — вложенный список: поле само по себе список записей (фотографии
    карточки, её метки). Глубина ограничена `BROWSER_SCENARIO_FIELDS_DEPTH`.
    """

    name: str = Field(min_length=1, max_length=50)
    selector: str = Field(min_length=1, max_length=500)
    attribute: str = Field('', max_length=50, description='Снять атрибут вместо текста')
    mandatory: bool = Field(False, description='Нет значения — строка не запись')
    fields: list['BrowserScenarioField'] = Field(default_factory=list)


class BrowserScenarioStep(BaseModel):
    """Один шаг. Поля общие на все действия, обязательные — по таблице выше."""

    action: str
    selector: str = Field('', max_length=500, description='CSS или text=… — ровно один элемент')
    url: str = Field('', max_length=1000)
    value: str = Field('', max_length=2000,
                       description='Текст, вариант списка, мс, пиксели; у extract «@src» — атрибут')
    key: str = Field('', max_length=50, description='Enter, Control+a')
    name: str = Field('', max_length=50, description='Под каким именем сложить результат extract')
    timeout_ms: int = Field(BROWSER_SCENARIO_STEP_TIMEOUT_MS, ge=100, le=120000)
    optional: bool = Field(False, description='Шаг может не сработать — прогон продолжится')
    fields: list[BrowserScenarioField] = Field(
        default_factory=list, description='Только у extract_rows: что брать внутри строки')

    @field_validator('action')
    @classmethod
    def _action_check(cls, value: str) -> str:
        if value not in BROWSER_SCENARIO_ACTIONS:
            raise ValueError(f'action: ожидается одно из {", ".join(sorted(BROWSER_SCENARIO_ACTIONS))}')
        return value

    @model_validator(mode='after')
    def _fields_check(self):
        for field in BROWSER_SCENARIO_ACTIONS.get(self.action, ()):
            value = getattr(self, field, '')
            # Списки проверяем как списки. Через `str()` пустой список даёт «[]» —
            # непустую строку, — и обязательное поле `fields` считалось бы
            # заполненным: `extract_rows` без единого поля проходил бы схему и
            # возвращал записи без содержимого.
            empty = not value if isinstance(value, list) else not str(value).strip()
            if empty:
                raise ValueError(f'{self.action}: не заполнено поле {field}')
        if self.action in ('wait_ms', 'scroll') and not self.value.strip().lstrip('-').isdigit():
            raise ValueError(f'{self.action}: value должно быть числом')
        # У `wait_stable` число необязательно (пусто — умолчание покоя), но если
        # оно есть, оно обязано быть числом: «2s» прошло бы схему и упало бы на
        # прогоне, потратив сессию.
        if self.action == 'wait_stable' and self.value.strip() and not self.value.strip().isdigit():
            raise ValueError('wait_stable: value должно быть числом миллисекунд')
        # Поля только у своего действия: у соседних они молча ничего не делали бы, а
        # автор сценария думал бы, что сбор настроен.
        if self.fields and self.action != 'extract_rows':
            raise ValueError(f'{self.action}: поля fields бывают только у extract_rows')
        if self.action == 'extract_rows':
            _fields_depth_check(self.fields, BROWSER_SCENARIO_FIELDS_DEPTH)
        return self


class BrowserScenario(BaseModel):
    """Сценарий целиком. `goal` — не украшение: по нему агент перепишет шаги,
    когда вёрстка сайта изменится, а цель останется прежней."""

    name: str = Field(..., min_length=1, max_length=100)
    goal: str = Field('', max_length=255)
    steps: list[BrowserScenarioStep] = Field(..., min_length=1, max_length=BROWSER_SCENARIO_STEP_LIMIT)


def browser_scenario_variables_used(scenario: BrowserScenario) -> list[str]:
    """Имена всех `{{переменных}}` сценария в порядке появления, без повторов."""
    names: list[str] = []

    for step in scenario.steps:
        for text in (step.selector, step.url, step.value, step.key):
            for name in BROWSER_SCENARIO_VAR_RE.findall(text):
                if name not in names:
                    names.append(name)

    return names


def browser_scenario_variables_missing(scenario: BrowserScenario, values: dict) -> list[str]:
    """Чего не хватает для прогона.

    Проверяется до первого шага, а не по ходу: иначе сценарий входа успел бы
    открыть форму и только на пароле сообщить, что его не передали.
    """
    return [name for name in browser_scenario_variables_used(scenario) if name not in values]


def browser_scenario_render(text: str, values: dict) -> str:
    """Подставляет значения. Неизвестное имя остаётся как есть — про него уже
    сказал `browser_scenario_variables_missing`, и второй раз падать незачем."""
    if not text:
        return text

    return BROWSER_SCENARIO_VAR_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


def browser_scenario_step_target(step: BrowserScenarioStep) -> str:
    """На что смотрел шаг — для журнала. Строка до подстановки: значения
    переменных в журнал не попадают, а `{{password}}` читается и так."""
    return step.url or step.selector or step.key or step.value


def _fields_depth_check(fields: list, left: int) -> None:
    """
    Вложенность полей не глубже отведённого. Иначе — отказ схемы, а не молчание.

    Считаем при разборе, а не на прогоне: сценарий с деревом полей вглубь прошёл бы
    сохранение и упал бы только у клиента, посреди сбора. Каждый уровень умножает
    число обращений к странице на число найденных строк, и глубина здесь — не вкус, а
    предел, за которым один шаг превращается в тысячи запросов.
    """
    if not fields:
        return
    if left <= 0:
        raise ValueError(f'extract_rows: поля вложены глубже {BROWSER_SCENARIO_FIELDS_DEPTH} уровней')
    for field in fields:
        _fields_depth_check(getattr(field, 'fields', None) or [], left - 1)

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Документация, докстринги и коммиты в этом репозитории — по-русски, идентификаторы — латиницей.
Этот файл следует тому же правилу.

## Что это за репозиторий

`py_modules` — **общий слой библиотек**, подключаемый submodule во все проекты сразу как
`<корень проекта>/py_modules`. Здесь нет приложения, точки входа и сюиты тестов: есть набор
namespace-папок, которые проекты импортируют напрямую.

Отсюда главное следствие, влияющее на каждую правку:

> **Правка здесь — правка всех проектов сразу, и их тесты здесь не гоняются.**

- Расширять функцию параметром с умолчанием — безопасно. **Менять сигнатуру, порядок
  параметров или имя публичной функции — нет:** соседние проекты зовут её по-старому и
  сломаются молча.
- Знание о конкретном проекте (таблицы, роли, состояния) сюда не попадает ни при каких
  условиях — модуль перестаёт быть переносимым для всех остальных.
- Коммит правки — **в этот репозиторий**, включая правки `docs/`. В проекте потом отдельным
  осознанным коммитом двигается указатель submodule.

## Правила, обязательные к соблюдению

Свод универсальных правил лежит в `docs/` — это **единственный источник**, в проекты он
приходит симлинком `.claude/docs-common -> ../py_modules/docs`. Оглавление — `docs/readme.md`.

Перед правкой **любого** кода здесь читать:

| Файл | Зачем |
|------|-------|
| `docs/code_rules.md` | именование, приватные внизу, дробление файлов — обязательно |
| `docs/py_modules.md` | границы слоя, двойной импорт, осторожность с сигнатурами |
| `docs/tool_rules.md`, §1 | куда писать код: общая библиотека или инструменты проекта |
| `docs/docs_rules.md` | формат документации, если правится `docs/` |

Дальше — по тому, какой namespace правится. Документа с описанием «как этот модуль
устроен и обо что об него уже спотыкались» может не быть — но если он есть, читать
его обязательно, иначе грабли собираются заново:

| Правится | Читать |
|----------|--------|
| `adb_/adb_.py` | `docs/mobile_device.md` — снимок, зрение, грабли скриншотов |
| `adb_/adb_ui.py`, `adb_input`, `adb_app`, `adb_log` | `docs/mobile_control.md` — карта экрана, нажатия, журнал |
| `adb_/adb_step.py`, `adb_state`, `adb_crop` | `docs/mobile_steps.md` — шаг с проверкой, замер, вырезка |
| `adb_/adb_cdp.py`, `adb_ws` | `docs/mobile_control.md`, §8 — WebView изнутри через CDP |
| `adb_/adb_emu.py` | `docs/mobile_emulator.md` — жизненный цикл AVD, сон экрана, пиксельные базы |
| `adb_/adb_burst.py` | `docs/mobile_device.md` — серия кадров с метками времени, тёмные кадры |
| `adb_/adb_rec.py` | `docs/mobile_rec.md` — запись экрана, разбор на кадры, обзорный лист, сверка двух записей |
| любой `adb_` с координатами и пикселями | `docs/mobile_hardware.md` — px/dp, даунсемплинг, почему координата уехала |
| `pdf_/` | `docs/pdf_tooling.md` — структурный осмотр, сверка, печать |
| `web_/web_shot.py` | `docs/web_shot.md` — раздача каталога, бюджет времени, inject-js, кроп кадра; там же про `web_probe.py` — JSON-зонд страницы через dump-dom (rect'ы, состояние, посев localStorage) |
| `browser_/browser_pool.py`, `browser_api*`, `browser_view*`, `browser_record`, `browser_inspect`, `browser_selector` | `docs/browser_control.md` — сессии-контексты, рестарт сервиса убивает страницы, скрипты в страницу, селекторы |
| `browser_/browser_scenario*.py` | `docs/browser_scenario.md` — действия, ожидание по признаку, секреты, сбор данных, трасса |
| `browser_/browser_read.py`, `browser_capture`, `browser_watch`, `browser_console`, `browser_cookie`, `browser_file`, `browser_wait` | `docs/browser_page.md` — вопросы к живой странице; **и `docs/web_shot.md`**: разовый кадр или зонд страницы уже умеет `web_` |
| `font_/` | `docs/font_tooling.md` — паспорт, покрытие, нормировка по upem |
| `image_/` | `docs/image_tooling.md` — замер яркости фона модой, подгонка пачки по эталону, аудит фона, проверка шва (поиск спокойной полосы, стороны прямоугольника), сравнение пары кадров, ведомость цветов разметки |
| `image_/image_scan.py` | `docs/image_scan.md` — полосы тона по разрезу, строки и шаг, габарит глифов |
| `image_/image_fix.py` | `docs/image_fix.md` — заплатки линейной интерполяцией с кромок, двумерная зачистка со стеклом и зерном (inpaint), штрих альфой из яркости |
| `image_/image_frames.py` | `docs/image_frames.md` — дельта скролла, полотно, drift, сдвиг и скорость полосы |
| `image_/image_layout.py` | `docs/image_layout.md` — геометрия блоков, точки-индикаторы, SDF-вырез |
| `image_/image_svg.py` | `docs/image_svg.md` — SVG в растр точного размера, суперсэмплинг, `magick`/`convert` |
| `image_/image_tone.py` | `docs/image_tone.md` — построчный профиль оттенка (медиана `g-b` по ярким), вердикт «полоса» |
| `image_/image_pair.py` | `docs/image_pair.md` — построчная сверка пары кадров (островки ручных правок), вшивка полосы без перегенерации |
| `apk_/apk_.py` | `docs/apk_inspect.md` — APK на машине: ресурсы, бинарный AXML, pathData векторов |
| `ai/provider/`, `ai/ai_thread.py` | `docs/llm_rules.md` — маршрутизация, приоритет, фоллбэк |
| `ai/ai_vision.py` | `docs/vision_llm.md` — контракт `data:`-URI, цена кадра, где модель врёт |
| `mysql_/` | `docs/database_rules.md`; дамп и выгрузка — ещё `docs/backup_rules.md` |
| `config/`, `setting_/` | `docs/env_config_rules.md` |
| `logging_/` | `docs/observability_rules.md` |
| `datetime_/` | `docs/datetime_rules.md` |
| `auth_/` | `docs/auth_rules.md` |
| `uvicorn_/` | `docs/api_rules.md` — контракт ответов, формат ошибок валидации |
| `project_/`, `mysql_/mysql_host.py`, `mysql_query.py` | `docs/project_runtime.md` |

Полное оглавление — `docs/readme.md`. Три файла оттуда к правкам здесь отношения не
имеют, они про устройство проекта-потребителя: `new_project.md`, `frontend_rules.md`,
`deploy_rules.md`. `testing_rules.md` — тоже про проект: сюита живёт там (см. «Команды»).

Короткая выжимка `code_rules.md` — то, что нарушают чаще всего:

- **Префикс публичной функции = namespace файла.** `mysql_/mysql_.py` → `mysql_pool_get()`,
  `adb_/adb_ui.py` → `adb_ui_find()`. Хвостовое подчёркивание в имени файла в префикс не
  входит. Единственная публичная функция, названная именем файла, — допустимое исключение.
- **Приватные (`_prefix`) — ниже всех публичных, в конце файла.**
- **Константы — `UPPER_CASE` с префиксом namespace**: `ADB_UI_WAIT_TIMEOUT`, `AI_VISION_MAX_SIDE`.
- **Никакой кириллицы в идентификаторах.** Докстринги и комментарии — по-русски.
- Ориентир по размеру файла — ~150–200 строк; больше — сигнал делить по ответственности.

**Исторические исключения не «чинят»** (переименование ломает соседей): `logger_info`,
`translate_text`, `add_text_to_image`, `auth_*` в `auth_/auth_primitive.py`.

Стиль коммита — как в истории: `<модуль>: <что изменилось и почему>`, например
`mysql_: разовый запрос к базе — адрес сам, пароль не в командной строке`.

## Сначала искать готовое, потом писать

Здесь больше двухсот публичных функций в трёх десятках namespace-папок, **общего реестра
и точки входа нет** — проекты импортируют нужное напрямую. Из-за этого второй вариант уже
существующей функции пишется незаметно: соседний namespace не виден, пока в него не
заглянешь.

**Правило: прежде чем добавлять функцию, файл или namespace — перечисли существующее.**
Оно работает в обе стороны: и на правку здесь, и на работу из проекта-потребителя —
самописный хелпер на Pillow, adb или ffmpeg в проекте почти всегда дубль того, что тут
уже есть, отлажено и имеет CLI. Цена дубликата не в том, что его дольше писать: грабли
в нём собираются заново — даунсемплинг снимков, ленивые импорты, скругления как источник
фальшивых границ всплывают через час отладки, хотя в готовой функции уже учтены.

Как перечислять — секунды, дешевле самописного аналога:

```bash
cat docs/readme.md                              # оглавление тем: строка на файл
ls                                              # namespace-папки: актуальнее любой таблицы
grep -rnP '^(async )?def [a-z]' --include=*.py . | grep -v '\.venv\|__pycache__'
grep -rln "__main__" --include=*.py . | grep -v '\.venv\|__pycache__'   # у чего есть CLI
PYTHONPATH=. python -m image_.image_scan --help  # что умеет кандидат
```

Что делать с находкой:

- **Похожее нашлось — расширяй его**, а не клади рядом второе: параметр с умолчанием
  безопасен (см. выше), вторая публичная функция с тем же смыслом расходится с первой
  и путает соседние проекты.
- **Нашлось в другом namespace — зови импортом**, а не копируй; тяжёлый стек подключай
  лениво внутри функции (`adb_` → `ai.ai_vision` именно так).
- **Не нашлось — сначала выбери слой**: универсальное сюда, знающее про таблицы и роли
  проекта — в его ветку инструментов. Критерий — `docs/tool_rules.md`, §1; имя нового —
  по правилу префикса namespace выше.

## Команды

Сборки, линтера и сюиты тестов в репозитории нет — ни `pyproject.toml`, ни `tests/`. Код
проверяют проекты-потребители своими интеграционными сюитами. Проверка правки **здесь** —
импорт модуля и вызов его CLI.

```bash
.venv/bin/python -c "from adb_.adb_ui import adb_ui_find"     # импорт: зависимости на месте
PYTHONPATH=. .venv/bin/python -m ai.ai_vision describe <файл>  # прогон CLI модуля
```

### Зависимости

Библиотеки объявлены **здесь**, рядом с кодом, а не угадываются в проекте. Ядро тощее
сознательно: снимок экрана не должен требовать LLM-стек.

```bash
pip install -r requirements.txt          # ядро: Pillow, numpy — картинки и пиксельные замеры
pip install -r requirements-vision.txt   # + зрение через ai_vision (pydantic-ai)
pip install -r requirements-doc.txt      # + документы: pdf_ (pypdf, pypdfium2), font_ (fontTools)
```

В requirements проекта — одна строка `-r py_modules/requirements.txt`; половину, которая
нужна этому проекту, он добирает своей строкой.

⚠️ Тяжёлые пакеты подключаются **лениво, внутри функций**, поэтому **импорт не проверяет
зависимости**: `from pdf_.pdf_ import pdf_info` пройдёт и там, где `pypdf` не стоит, —
`ModuleNotFoundError` прилетит из вызова `pdf_info(...)`. Проверка правки в `pdf_`, `font_`
и vision-половине `ai/` — вызов функции или CLI; одного импорта мало.

CLI есть у модулей, которыми пользуются «руками» (`python -m <пакет>.<модуль> <команда>`):

| Модуль | Команды |
|--------|---------|
| `project_.project_` | `root`, `main-root`, `python`, `env` |
| `project_.project_run` | `-c` / `-a` / `-m` / `<файл>` / `-` (stdin) |
| `mysql_.mysql_query` | `address`, `tables`, `columns <таблица>`, `sql '<запрос>' [--write] [--format table\|json\|csv] [--limit N]` |
| `ai.ai_vision` | `describe`, `normalize` |
| `adb_.adb_` | `devices`, `info`, `size`, `capture`, `describe` |
| `adb_.adb_ui` | `map`, `find`, `dump` |
| `adb_.adb_input` | `tap`, `tap-on`, `swipe`, `scroll`, `text`, `key`, `wake`, `wake-full [no-unlock no-home no-stayon]`, `stayon on\|off` |
| `adb_.adb_app` | `current`, `list`, `start`, `stop`, `version`, `wait`, `install <apk> [-d]`, `pull-apk <пакет> [--out каталог]` |
| `adb_.adb_log` | `read`, `crash`, `clear`, `pid` |
| `adb_.adb_file` | `push <local> <remote>`, `pull <remote> [каталог]`, `ls <dir> [--pattern glob]`, `latest <dir> [--pattern glob]` |
| `adb_.adb_doc` | `save [--dir Download]` |
| `adb_.adb_step` | `tap`, `scroll`, `key`, `text`, `look` |
| `adb_.adb_state` | `read`, `settle`, `last` |
| `adb_.adb_crop` | `on`, `box`, `part` |
| `adb_.adb_emu` | `up`, `ready`, `sleep`, `kill` |
| `adb_.adb_cdp` | `connect`, `pages`, `target <часть адреса>`, `eval`, `navigate`, `capture`, `element`, `element-rect`, `element-shot`, `viewport`, `tap <селектор>`, `storage КЛЮЧ='{json}' [--reload]` |
| `adb_.adb_burst` | `capture [--n --interval --outdir --during «shell»]`, `launch ПАКЕТ [--activity --n --interval]`, `timeline [КАТАЛОГ] [--band Y0 Y1 X0 X1 --probe Y,X] [--serial ...]` |
| `adb_.adb_rec` | `record [out] [--seconds N --during «shell» --serial S]`, `frames [mp4] [--times 0.5,1.2 │ --fps 2 --out-dir DIR]`, `sheet [mp4] [--out --fps --cols --width]`, `compare <a> <b> --times 0.3,1.4 [--labels A,B --cell-width --out]` |
| `web_.web_shot` | `<URL или HTML> [--out --size Wxч --budget мс --inject-js --crop x0,ч0,x1,ч1]` |
| `web_.web_probe` | `<HTML или URL> (--rect CSS …│--probe-js «тело») [--seed-js --size --budget]` → JSON |
| `browser_.browser_` | `read <url>`, `snapshot <url>`, `shot <url> <файл>`, `pdf <url> <файл>`, `run <сценарий.json> [--var имя=значение]` |
| `pdf_.pdf_` | `info`, `text`, `render`, `diff`, `diff-multi`, `whiteout`, `print`, `extract` |
| `pdf_.pdf_spans` | `get <файл> [--page N]` |
| `pdf_.pdf_xobject` | `list <файл> [--page N]` |
| `font_.font_` | `info`, `coverage`, `compare`, `render`, `ink`, `fit`, `textdiff`, `baseline-top` |
| `image_.image_` | `measure`, `match`, `audit`, `seam`, `diff`, `rect-seams`, `literals` |
| `image_.image_scan` | `runs`, `bbox [--tone --tol --rect --alpha-thresh]`, `rows`, `glyph`, `windows`, `diff` |
| `image_.image_svg` | `<svg> [-o out] [--size 512 --supersample 3]` |
| `image_.image_fix` | `erase <файл> --out --box ...`, `inpaint <файл> --out --box ... [--sigma]`, `fill <файл> --out --box --color`, `strokes <файл> --out --box ...` |
| `image_.image_frames` | `delta`, `stitch`, `drift`, `shift`, `grid` |
| `image_.image_tone` | `rows <файл> [--rect --chan --min-luma --step]`, `band <файл> [... --min-jump]` |
| `image_.image_pair` | `diff-rows <a> <b> [--thresh --axis --rect]`, `splice <base> <patch> --out --pos [--axis --quality --method]` |
| `apk_.apk_` | `entries <apk> [glob]`, `dump <apk> [подстрока] [--config default│'']`, `xml <apk> <запись>`, `pathdata <apk> <запись>`, `extract <apk> <запись> [--out-dir --png]` |
| `i18n_.i18n_` | `<український текст>` (транслітерація КМУ № 55) |

Список сверяется командой — таблица устаревает быстрее кода:

```bash
grep -rln "__main__" --include=*.py . | grep -v '\.venv\|__pycache__'
```

Машинные проверки соглашений (вывод обязан быть пустым; полные версии — в
`docs/code_rules.md`, раздел «Автопроверка»):

```bash
# приватная функция стоит выше публичной
for f in $(git ls-files '*.py'); do
  last_pub=$(grep -nP '^(async )?def [a-z]' "$f" | tail -1 | cut -d: -f1)
  first_priv=$(grep -nP '^(async )?def _' "$f" | head -1 | cut -d: -f1)
  [ -n "$last_pub" ] && [ -n "$first_priv" ] && [ "$first_priv" -lt "$last_pub" ] && echo "$f"
done

# кириллица в именах
grep -rnP "^\s*(async\s+)?def\s+\w*[а-яА-ЯёЁ]|^\s*class\s+\w*[а-яА-ЯёЁ]" \
  --include=*.py . | grep -v "\.venv\|__pycache__"
```

Известные несоответствия на сегодня: `qdrant_/qdrant_.py` (приватная выше публичной) —
чинится при следующей правке этого файла, «согласованных исключений» не заводят.

## Архитектура

### Импорт — только короткой формой

```python
from mysql_.mysql_ import mysql_get_db_async              # так — везде
from py_modules.mysql_.mysql_ import mysql_get_db_async   # так — никогда
```

В `PYTHONPATH` проекта лежат оба корня (`/app` и `/app/py_modules`), поэтому модуль виден под
двумя именами и **загружается дважды**: два объекта модуля, два набора модульных переменных.
У `mysql_` на уровне модуля лежит `pool` — значит два независимых пула соединений вместо
одного. Модульные синглтоны есть также у `logging_` (логгер, `trace_id`) и `queue_`/`state`.

### Корень проекта считается от файла, а не от рабочего каталога

`config`, `project_` и всё, что от них зависит, берут корень как `Path(__file__).parents[2]` —
из расчёта `<корень>/py_modules/<пакет>/<файл>.py`. Обход вверх от cwd границы репозитория не
знает и находит чужой `.env` в домашнем каталоге (так уже было).

⚠️ **В самостоятельной выкачке этого репозитория** (как здесь, `~/PycharmProjects/py_modules`)
формула даёт **родительский каталог**: `project_root()` вернёт `~/PycharmProjects`, а `config`
поищет `.env` там же. Всё, что зависит от окружения проекта — `config_get`, `mysql_*`,
`setting_`, — осмысленно проверяется только внутри настоящего проекта, где репозиторий
подключён submodule.

Точки входа, которые обязаны работать при запуске по прямому пути
(`mysql_/mysql_query.py`, `project_/project_run.py`), чинят себе `sys.path` первыми строками:
запуск файла по пути кладёт в путь поиска **его каталог**, а не корень.

### Слои и направление зависимостей

`config` и `logging_` — фундамент, их импортируют почти все. Обратной зависимости нет.
Дальше — плоский набор независимых namespace-папок; общего реестра или точки входа не
существует, проект импортирует нужное напрямую. Что где лежит:

| Слой | Namespace |
|------|-----------|
| Фундамент | `config`, `logging_` |
| Хранилища | `mysql_` (пул, репозитории, дамп, миграции), `redis_`, `redis_queue`, `qdrant_`, `sqllite3`, `queue_`, `state` |
| LLM | `ai/provider` (реестр сервисов), `ai/framework` (абстракции движка), `ai/ai_thread`, `ai/ai_vision`, `mcp_` |
| Обвязка приложения | `setting_`, `event_`, `auth_`, `uvicorn_`, `i18n_` |
| Утилиты | `project_`, `datetime_`, `json_`, `async_`, `thread_` |
| Внешнее и файлы | `adb_` (устройство), `web_` (headless-рендер страницы), `pdf_`, `font_`, `apk_` (разбор APK на машине), `translator`, `flux_schnell`, `microphone` |

Назначение каждого — `docs/py_modules.md`, §3; актуальный список — всегда `ls`, а не эта
таблица.

Тяжёлые зависимости подключаются **лениво, внутри функции**, чтобы соседний модуль не тянул
чужой стек: `adb_` импортирует `ai.ai_vision` только в момент распознавания (снимок экрана не
должен требовать pydantic-ai), а `ai_vision` импортирует реестр провайдеров только в момент
запроса (нормализация JPEG живёт без него). По той же схеме `pdf_` берёт `pypdf`/`pypdfium2`,
а `font_` — `fontTools`: на уровне модуля у них только stdlib.

Цена приёма: **импорт больше не проверяет зависимости** — `from pdf_.pdf_ import pdf_info`
пройдёт и там, где `pypdf` не стоит. Проверка правки в таком модуле — вызов функции или CLI,
одного импорта мало (см. «Зависимости»).

### `ai/` — провайдеры LLM как стратегии

Имя модели несёт сервис: `openrouter/deepseek/deepseek-v4-flash-0731`, до первого слэша —
кто её обслуживает. Разбор имени общий, особенности сервиса — в своём файле.

- `ai/provider/ai_provider.py` — база `AiProvider`: `model_get()` (объект модели для
  pydantic-ai) и сырой путь мимо него — `raw_endpoint()` (адрес и ключ по соглашению
  `<СЕРВИС>_API_URL` / `_API_KEY`) и `raw_body_no_thinking()`.
- `ai/provider/ai_provider_registry.py` — соответствие «префикс → класс», перечислено
  **явно**, а не обходом модуля: список сервисов часть контракта, незнакомое имя обязано
  давать внятный отказ. **Новый сервис = новый файл + строка в реестре**, а не ещё одна ветка
  `if` в общей функции (из такой ветки уже вырос дефект: запрет размышлений не доезжал до
  моделей за OpenRouter).
- Запрет размышлений — единым ключом `thinking` (`AiProvider.thinking_settings`), pydantic-ai
  сама переводит его в `reasoning_effort` или `thinking_budget=0`. Разбирать сервисы руками
  здесь не нужно.

`ai/framework/` — **абстракции для проекта, а не движок**: `AbstractAiFramework`,
`AbstractAiFrameworkManager`, `AiFrameworkModel`. Конкретный движок сценариев знает таблицы и
шаги проекта и живёт в проекте, наследуясь отсюда. Та же граница у
`mysql_/repository/abstract_repository.py`: базовый CRUD здесь, `table_name_get()` — в проекте.

### `browser_/` — сервис целиком, а не набор функций

Единственный пакет здесь, который проект не «зовёт», а **поднимает**: Chromium живёт в
своём процессе, сессия — это `BrowserContext`, реестр лежит в памяти. Проекту остаётся
точка входа (`browser_pool_start` в lifespan) и один роутер (`browser_router`).

Отсюда два следствия, которых нет у остальных пакетов:

- **правка здесь требует рестарта браузерного сервиса и убивает все его сессии** — код
  живёт дольше, чем страницы, которые на нём открыты;
- **новая ручка появляется сразу у всех проектов**, потому что состав роутера — дело
  этого пакета, а не их точек входа.

Схема шагов сценария (`browser_scenario.py`) не тянет ни Playwright, ни базу намеренно:
её читают обе стороны — тот, кто шаги выполняет, и тот, кто их хранит и проверяет при
сохранении. Правила — `docs/browser_control.md`, `docs/browser_scenario.md`,
`docs/browser_page.md`.

### Разовое действие в проекте

`project_` и `mysql_host`/`mysql_query` существуют, чтобы не собирать составную команду с `cd`,
heredoc и паролем в аргументах: они отвечают на вопросы «где корень», «каким интерпретатором»,
«что в PYTHONPATH», «какой адрес базы рабочий». Контракты и грабли — `docs/project_runtime.md`.
Отдельно про два корня: `project_root()` — своё, `project_main_root()` — всё, что помнит проект
по каталогу (docker compose), иначе из worktree оркестратор отвечает «сервис не запущен».

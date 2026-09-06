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
| любой `adb_` с координатами и пикселями | `docs/mobile_hardware.md` — px/dp, даунсемплинг, почему координата уехала |
| `pdf_/` | `docs/pdf_tooling.md` — структурный осмотр, сверка, печать |
| `font_/` | `docs/font_tooling.md` — паспорт, покрытие, нормировка по upem |
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
```

В requirements проекта — одна строка `-r py_modules/requirements.txt`.

⚠️ `pdf_` и `font_` тянут `pypdf`, `pypdfium2`, `fontTools` **лениво, внутри функций**, и
эти пакеты не объявлены ни в одном requirements — в venv их может не быть (в выкачке
репозитория на сегодня нет). Импорт модуля при этом проходит, падает только вызов
функции: `ModuleNotFoundError` из `pdf_info`, а не при `from pdf_.pdf_ import pdf_info`.

CLI есть у модулей, которыми пользуются «руками» (`python -m <пакет>.<модуль> <команда>`):

| Модуль | Команды |
|--------|---------|
| `project_.project_` | `root`, `main-root`, `python`, `env` |
| `project_.project_run` | `-c` / `-a` / `-m` / `<файл>` / `-` (stdin) |
| `mysql_.mysql_query` | `address`, `tables`, `columns <таблица>`, `sql '<запрос>' [--write] [--format table\|json\|csv] [--limit N]` |
| `ai.ai_vision` | `describe`, `normalize` |
| `adb_.adb_` | `devices`, `info`, `size`, `capture`, `describe` |
| `adb_.adb_ui` | `map`, `find`, `dump` |
| `adb_.adb_input` | `tap`, `tap-on`, `swipe`, `scroll`, `text`, `key`, `wake` |
| `adb_.adb_app` | `current`, `list`, `start`, `stop`, `version`, `wait` |
| `adb_.adb_log` | `read`, `crash`, `clear`, `pid` |
| `adb_.adb_step` | `tap`, `scroll`, `key`, `text`, `look` |
| `adb_.adb_state` | `read`, `settle`, `last` |
| `adb_.adb_crop` | `on`, `box`, `part` |
| `adb_.adb_emu` | `up`, `ready`, `sleep`, `kill` |
| `adb_.adb_cdp` | `connect`, `pages`, `eval`, `navigate` |
| `pdf_.pdf_` | `info`, `text`, `render`, `diff`, `whiteout`, `print`, `extract` |
| `font_.font_` | `info`, `coverage`, `compare`, `render`, `textdiff` |

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
| Внешнее и файлы | `adb_` (устройство), `pdf_`, `font_`, `translator`, `flux_schnell`, `microphone` |

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

### Разовое действие в проекте

`project_` и `mysql_host`/`mysql_query` существуют, чтобы не собирать составную команду с `cd`,
heredoc и паролем в аргументах: они отвечают на вопросы «где корень», «каким интерпретатором»,
«что в PYTHONPATH», «какой адрес базы рабочий». Контракты и грабли — `docs/project_runtime.md`.
Отдельно про два корня: `project_root()` — своё, `project_main_root()` — всё, что помнит проект
по каталогу (docker compose), иначе из worktree оркестратор отвечает «сервис не запущен».

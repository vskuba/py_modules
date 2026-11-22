# dsh board — агенты DeepSeek Harness субагентами

> Оркестратор на той же машине, что и хост harness'а (Claude Code), раздаёт работу его
> агентам так же, как человек в браузере: заводит карточку под проект, шлёт промпт, ждёт
> ответ. Повторяющаяся работа ложится на доску плагина `dsh-continuous-agent` и идёт
> очередью и по расписанию сама. `dsh_/dsh_session.py` — карточки и их журнал,
> `dsh_/dsh_board.py` — задачи, потолок, доска, `dsh_/dsh_board_cli.py` — команда.

Требования: хост `dsh --profile web` на этой машине (транспорт — `docs/dsh_api.md`) и
плагин `dsh-continuous-agent` из claude-shell (`plugins/`). Без плагина работают только
функции `dsh_session`.

## 1. Задача субагенту

```python
from dsh_.dsh_board import dsh_board_session_new, dsh_board_ask

session = dsh_board_session_new('~/PycharmProjects/BlogAi', 'разбор логов')
answer = dsh_board_ask(session, 'найди ошибки в logs/ и предложи фикс', wait=True)
```

Карточка заводится как кнопкой «＋ сессия» доски — preset и модель хоста по умолчанию.
Без `wait` промпт встаёт в очередь карточки: несколько вызовов подряд — очередь
промптов, агент разберёт их по одному; ответ потом — `dsh_session_answer_last`.

## 2. Много задач разом

```python
from dsh_.dsh_board import dsh_board_fanout

results = dsh_board_fanout([
    {'session': session, 'text': 'прогони тесты'},
    {'cwd': '~/PycharmProjects/CyberAi', 'title': 'ревью', 'text': '…', 'archive': True},
])
# [{'session': …, 'answer': …, 'error': ''}, …] — в порядке задач
```

Сорвавшаяся задача остальных не роняет — у неё заполнен `error`. `archive: true` убирает
в архив карточку, которую `fanout` завёл сам; чужую (`session`) не трогает никогда.

## 3. Потолок сессий разом

Настройка доски `maxParallel` (0 — без потолка) считает **все** занятые сессии хоста:
заходы доски, работу руками и наши промпты. Бегунок доски соблюдает её сам;
`dsh_board_ask` — со своей стороны: место проверяется и промпт уходит под файловой
блокировкой `<DSH_HOME>/dsh-board.lock`, которая держится до начала круга. Карточка,
уже занятая работой, места не требует — промпт встанет в её очередь.

```python
from dsh_.dsh_board import dsh_board_settings
dsh_board_settings({'maxParallel': 4})
```

## 4. Доска промптов

Правка целей — `dsh_board_tool(session, 'continuous_add' | 'continuous_edit' |
'continuous_drop', args)`: те же инструменты, что у модели, от имени живой карточки.
Снимок — `dsh_board_state()`: цели, цепочки, занятые машины (`machines`), настройки,
архивные карточки (`archived`).

## 5. Команда

```bash
PYTHONPATH=. python -m dsh_.dsh_board sessions
PYTHONPATH=. python -m dsh_.dsh_board new ~/proj "логи" --ask "найди ошибки" --wait
PYTHONPATH=. python -m dsh_.dsh_board ask <session> "прогони тесты" --wait
PYTHONPATH=. python -m dsh_.dsh_board fanout tasks.json          # `-` — stdin
PYTHONPATH=. python -m dsh_.dsh_board history <session> -n 5
PYTHONPATH=. python -m dsh_.dsh_board board --session <session>
PYTHONPATH=. python -m dsh_.dsh_board add <session> "сверяй логи" --every 60
PYTHONPATH=. python -m dsh_.dsh_board limit 4
```

Проект ставит её своей командой одной строкой — claude-shell: `dsh-board`
(`src/dsh/dsh_board.py` зовёт `dsh_board_cli_main`).

## Грабли

1. **Конец круга — по журналу, не опросом «идёт ли круг».** Перед отправкой берётся
   засечка журнала, готово — когда после неё лёг `turn/end`. Опрос `running` пропускает
   короткий круг (3–5 с), если хост в эти секунды занят: замечено, что после первого
   круга новой карточки `/api` молчит до 30 с.
2. **Журнал без вебсокета.** Хвост журнала хост отдаёт первым кадром потока
   `session/follow`, но тот же хвост даёт обычный `session/page` — ему нужна засечка
   `throughSeq` не дальше текущей (иначе `bad-request … past cursor`). Её приносит
   `session/list`: `projections.asOfSeq` карточки.
3. **Архивная карточка остаётся в `session/list`.** Архив виден только реестром
   проектов (поток `workspace/follow`); доска отдаёт его полем `archived` в `state`.
4. **Промпт человека — `user/message` с `source.kind == "user"`.** Тем же типом идут
   служебные вставки хоста (инструкции проекта, снимок песочницы) со своим `kind`.
5. **Без модели цель доски «неположена»** и не идёт нигде — кладите её на цепочку
   модели своей карточки (`sessionModel` из `state`), как делает форма.

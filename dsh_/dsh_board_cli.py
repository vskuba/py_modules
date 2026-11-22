"""Командная строка `dsh_board`: сессии, задачи субагентам, ответы, доска промптов.

Тонкий разбор аргументов над `dsh_board` и `dsh_session` — решений здесь нет. Проекты
ставят его своей командой одной строкой (claude-shell: `dsh-board`):

```bash
PYTHONPATH=. python -m dsh_.dsh_board sessions                  # живые карточки
PYTHONPATH=. python -m dsh_.dsh_board new ~/proj "логи" --ask "найди ошибки" --wait
PYTHONPATH=. python -m dsh_.dsh_board ask <session> "прогони тесты" --wait
echo "длинный промпт" | PYTHONPATH=. python -m dsh_.dsh_board ask <session> -
PYTHONPATH=. python -m dsh_.dsh_board fanout tasks.json          # N задач разом
PYTHONPATH=. python -m dsh_.dsh_board history <session> -n 5
PYTHONPATH=. python -m dsh_.dsh_board board --session <session>  # очередь промптов карточки
PYTHONPATH=. python -m dsh_.dsh_board add <session> "сверяй логи" --every 60 --motor 2
PYTHONPATH=. python -m dsh_.dsh_board motors gx10-1/Qwen3.8-Flash-Next 1   # llama — один поток
```

`--json` — ответ JSON вместо текста, ставится и до, и после подкоманды.
"""
import argparse
import json
import os
import sys
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DshApiError
from dsh_.dsh_board import DSH_BOARD_BRIEF_ASK, DSH_BOARD_WAIT_TIMEOUT, DshBoardError, \
    dsh_board_ask, dsh_board_brief, dsh_board_inbox, dsh_board_inbox_take, \
    dsh_board_fanout, dsh_board_goal_session, dsh_board_model_of, dsh_board_motors_set, \
    dsh_board_session_new, dsh_board_working, \
    dsh_board_sessions, dsh_board_settings, dsh_board_state, dsh_board_tool
from dsh_.dsh_session import dsh_session_answer_last, dsh_session_archive, \
    dsh_session_cancel, dsh_session_cursor, dsh_session_dialog


def dsh_board_cli_main(argv: list = None, prog: str = 'dsh-board') -> int:
    """Разобрать аргументы и исполнить команду; код выхода для `sys.exit`."""
    parser = _parser(prog)
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        return _run(args)
    except (DshBoardError, DshApiError, ValueError, OSError) as error:
        print(f'{prog}: {error}', file=sys.stderr)
        return 1


# ── детали реализации ──

def _run(args) -> int:
    base = args.base
    command = args.command

    if command == 'sessions':
        return _print(args, dsh_board_sessions(args.all, base=base), _sessions_text)

    if command == 'new':
        session = dsh_board_session_new(args.cwd, args.title, args.full_access, base=base)
        if args.ask is None:
            return _print(args, {'session': session}, lambda v: v['session'])
        answer = dsh_board_ask(session, _asked(args, args.ask), args.wait, args.timeout, args.image,
                               base=base)
        return _print(args, _answer_row(args, session, answer),
                      lambda v: f"{v['session']}\n{_answer_text(v)}".rstrip())

    if command == 'ask':
        answer = dsh_board_ask(args.session, _asked(args, args.text), args.wait, args.timeout, args.image,
                               base=base)
        return _print(args, _answer_row(args, args.session, answer),
                      lambda v: _answer_text(v) or 'принято в очередь карточки')

    if command == 'fanout':
        raw = sys.stdin.read() if args.file == '-' else open(args.file, encoding='utf-8').read()
        tasks = json.loads(raw)
        if not isinstance(tasks, list):
            raise ValueError('fanout ждёт JSON-список задач')
        if args.out_dir:
            tasks = [{**task, 'text': task['text'] + DSH_BOARD_BRIEF_ASK} for task in tasks]
        rows = dsh_board_fanout(tasks, args.timeout, base=base)
        if args.out_dir:
            os.makedirs(args.out_dir, exist_ok=True)
            for index, row in enumerate(rows, 1):
                path = os.path.join(args.out_dir, f'{index}.md')
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write(row['answer'] or row['error'])
                row.update(out=path, brief=dsh_board_brief(row['answer']))
                row.pop('answer')
        return _print(args, rows, _fanout_text)

    if command == 'working':
        rows = dsh_board_working(base=base)
        return _print(args, rows, _working_line if args.line else _working_text)

    if command == 'inbox':
        rows = dsh_board_inbox(base=base)
        if args.take:
            for row in rows:
                dsh_board_inbox_take(row['session'], row['seq'])
        return _print(args, rows, _inbox_text)

    if command == 'answer':
        answer = dsh_session_answer_last(args.session, base=base)
        dsh_board_inbox_take(args.session, dsh_session_cursor(args.session, base=base))
        return _print(args, {'session': args.session, 'answer': answer}, lambda v: v['answer'])

    if command == 'history':
        return _print(args, dsh_session_dialog(args.session, args.n, base=base), _history_text)

    if command == 'cancel':
        dsh_session_cancel(args.session, base=base)
        return _print(args, {'ok': True}, lambda v: 'отменено')

    if command == 'archive':
        dsh_session_archive(args.session, base=base)
        return _print(args, {'ok': True}, lambda v: 'в архиве')

    if command == 'board':
        state = dsh_board_state(args.session or '', base=base)
        if args.session:
            state = {**state, 'goals': [goal for goal in state.get('goals', [])
                                        if goal.get('session') == args.session]}
        return _print(args, state, _board_text)

    if command == 'add':
        facets = _facets(args)
        if 'model' not in facets:
            # Без модели промпт «неположенный» и не идёт нигде — кладём в бокс модели
            # своей карточки (у свежей — единственной включённой), как форма с черновиком.
            model = dsh_board_model_of(args.session, base=base)
            if model:
                facets['model'] = model
        value = dsh_board_tool(args.session, 'continuous_add',
                               {'text': _text(args.text), **facets}, base=base)
        return _print(args, value, lambda v: _goal_added_text(v, args.session))

    if command == 'edit':
        session = dsh_board_goal_session(args.goal, base=base)
        patch = _facets(args)
        if args.text is not None:
            patch['text'] = _text(args.text)
        value = dsh_board_tool(session, 'continuous_edit', {'goal': args.goal, **patch}, base=base)
        return _print(args, value, lambda v: f'цель {args.goal} поправлена')

    if command == 'drop':
        session = dsh_board_goal_session(args.goal, base=base)
        value = dsh_board_tool(session, 'continuous_drop', {'goal': args.goal}, base=base)
        return _print(args, value, lambda v: f'цель {args.goal} убрана')

    if command == 'motors':
        provider, _, model = args.model.partition('/')
        if not provider or not model:
            raise ValueError('модель пишется как provider/model, например gx10-2/Qwen3.8-Flash-Next')
        dsh_board_motors_set(provider, model, args.count, base=base)
        return _print(args, {'provider': provider, 'model': model, 'motors': args.count},
                      lambda v: f"моторов у {v['provider']}/{v['model']}: {v['motors']}")

    if command in ('pause', 'resume'):
        dsh_board_settings({'paused': command == 'pause'}, base=base)
        return _print(args, {'paused': command == 'pause'},
                      lambda v: 'доска на паузе' if v['paused'] else 'доска работает')

    raise ValueError(f'неизвестная команда {command}')


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description='Сессии, задачи субагентам и доска промптов DeepSeek Harness.')
    parser.add_argument('--base', default=DSH_API_ADDR_DEFAULT, help='host:port хоста')
    parser.add_argument('--json', action='store_true', help='ответ JSON вместо текста')
    # `--json` понимают и после подкоманды: `dsh-board board --json` читается естественнее.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                        help='ответ JSON вместо текста')
    sub = parser.add_subparsers(dest='command')

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text)

    command('sessions', 'карточки хоста с живым агентом').add_argument(
        '--all', action='store_true', help='и без живого агента')

    new = command('new', 'завести карточку под каталог проекта')
    new.add_argument('cwd')
    new.add_argument('title')
    new.add_argument('--full-access', action='store_true', help='полный доступ карточке')
    new.add_argument('--ask', default=None, help='сразу отправить промпт (`-` — stdin)')
    _wait_args(new)

    ask = command('ask', 'задача субагенту: промпт в очередь карточки')
    ask.add_argument('session')
    ask.add_argument('text', help='текст промпта; `-` — читать stdin')
    _wait_args(ask)

    fanout = command('fanout', 'раздать задачи разом и собрать ответы')
    fanout.add_argument('file', help='JSON-список [{text, session | cwd+title, archive?}]; `-` — stdin')
    fanout.add_argument('--timeout', type=float, default=DSH_BOARD_WAIT_TIMEOUT)
    fanout.add_argument('--out-dir', default=None, metavar='КАТАЛОГ',
                        help='полные ответы — в КАТАЛОГ/N.md, в вывод — только итоги')

    command('working', 'кто из агентов работает сейчас и над чем').add_argument(
        '--line', action='store_true', help='одной строкой — для статусной строки')

    command('answer', 'последний ответ карточки (и отметить прочитанным)').add_argument('session')
    command('inbox', 'агентские карточки с непрочитанными ответами').add_argument(
        '--take', action='store_true', help='отметить показанное прочитанным')

    history = command('history', 'промпты и ответы карточки')
    history.add_argument('session')
    history.add_argument('-n', type=int, default=10, help='сколько последних реплик')

    command('cancel', 'прервать идущий круг карточки').add_argument('session')
    command('archive', 'убрать карточку в архив').add_argument('session')

    command('board', 'доска: промпты, статусы, круги, моторы').add_argument(
        '--session', default=None, help='только промпты этой карточки')

    add = command('add', 'промпт на доску от имени карточки')
    add.add_argument('session')
    add.add_argument('text', help='текст промпта; `-` — читать stdin')
    _facet_args(add)

    edit = command('edit', 'поправить промпт доски')
    edit.add_argument('goal')
    edit.add_argument('--text', default=None, help='новый текст; `-` — stdin')
    _facet_args(edit)

    command('drop', 'убрать промпт с доски насовсем').add_argument('goal')
    motors = command('motors', 'сколько параллельных цепочек у модели (по умолчанию 4)')
    motors.add_argument('model', help='provider/model')
    motors.add_argument('count', type=int, choices=[1, 2, 3, 4])

    command('pause', 'рубильник: остановить всю работу доски')
    command('resume', 'рубильник: продолжить работу доски')
    return parser


def _wait_args(parser) -> None:
    parser.add_argument('--wait', action='store_true', help='дождаться ответа')
    parser.add_argument('--out', default=None, metavar='ФАЙЛ',
                        help='полный ответ — в ФАЙЛ, в вывод — только итог (бережёт контекст)')
    parser.add_argument('--image', action='append', default=[], metavar='ПУТЬ',
                        help='картинка к промпту (png/jpeg/webp/gif); флаг повторяется')
    parser.add_argument('--timeout', type=float, default=DSH_BOARD_WAIT_TIMEOUT,
                        help=f'потолок ожидания, с (по умолчанию {DSH_BOARD_WAIT_TIMEOUT})')


def _facet_args(parser) -> None:
    parser.add_argument('--every', type=int, default=None,
                        help='период в минутах; 0 — одноразовый (по умолчанию)')
    parser.add_argument('--quota', type=int, default=None, help='кругов на заход')
    parser.add_argument('--status', choices=['active', 'paused', 'done', 'blocked'], default=None)
    parser.add_argument('--model', default=None, help='цепочка: provider/model')
    parser.add_argument('--motor', type=int, choices=[1, 2, 3, 4], default=None, help='ряд цепочки, 1–4')
    parser.add_argument('--position', type=int, default=None, help='место в очереди, с 1')


def _facets(args) -> dict:
    """Поля цели из флагов — только названные: пропущенное инструмент оставляет как было."""
    facets = {}
    for flag, key in (('every', 'everyMinutes'), ('quota', 'quotaRounds'),
                      ('status', 'status'), ('motor', 'motor'), ('position', 'position')):
        value = getattr(args, flag)
        if value is not None:
            facets[key] = value
    if args.model:
        provider, _, model = args.model.partition('/')
        if not provider or not model:
            raise ValueError('--model пишется как provider/model, например gx10-2/Qwen3.8-Flash-Next')
        facets['model'] = {'provider': provider, 'model': model}
    return facets


def _text(value: str) -> str:
    text = sys.stdin.read() if value == '-' else value
    if not text.strip():
        raise ValueError('пустой текст промпта')
    return text


def _print(args, value, as_text) -> int:
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(as_text(value))
    return 0


def _clock(ms) -> str:
    return time.strftime('%d.%m %H:%M', time.localtime(ms / 1000)) if ms else '—'


def _sessions_text(rows: list) -> str:
    if not rows:
        return 'живых карточек нет'
    return '\n'.join(f"{'▶' if row['running'] else '·'} {row['session']}  "
                     f"{row['title'] or '—'}  [{row['cwd']}]" for row in rows)


def _history_text(rows: list) -> str:
    if not rows:
        return 'сообщений нет'
    return '\n\n'.join(f"{'▶ промпт' if row['role'] == 'user' else '◀ ответ'} · "
                       f"{_clock(row['time'])}\n{row['text']}" for row in rows)


def _working_text(rows: list) -> str:
    if not rows:
        return 'никто не работает'
    return '\n'.join(f"мотор {row['motor']}  {row['title'] or row['session']}"
                     f"{'  — ' + row['goal'] if row['goal'] else '  — работа руками'}" for row in rows)


def _working_line(rows: list) -> str:
    """`DSH 2: ревью dsh_board · Часовая сверка…` — пусто, если никто не работает."""
    if not rows:
        return ''
    parts = [_short(row['goal'] or row['title'] or row['session'], 28) for row in rows]
    return f"DSH {len(rows)}: {' · '.join(parts)}"


def _short(text: str, limit: int) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _fanout_text(rows: list) -> str:
    blocks = []
    for index, row in enumerate(rows, 1):
        head = f"#{index} {row['session'] or '—'}"
        if row['error']:
            blocks.append(f"{head}  ✗ {row['error']}")
        elif 'out' in row:
            blocks.append(f"{head}\n{row['brief']}\n(полный ответ: {row['out']})")
        else:
            blocks.append(f"{head}\n{row['answer']}")
    return '\n\n'.join(blocks)


def _inbox_text(rows: list) -> str:
    if not rows:
        return 'непрочитанных ответов нет'
    return '\n\n'.join(f"🤖 {row['title'] or row['session']}  [{row['session']}]\n{row['brief']}"
                       for row in rows)


def _asked(args, value: str) -> str:
    """Текст промпта; с `--out` — с просьбой начать ответ итогом."""
    text = _text(value)
    return text + DSH_BOARD_BRIEF_ASK if getattr(args, 'out', None) else text


def _answer_row(args, session: str, answer: str) -> dict:
    """Строка ответа; с `--out` полный ответ уходит в файл, в строке — итог и путь."""
    if not getattr(args, 'out', None) or not answer:
        return {'session': session, 'answer': answer}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as handle:
        handle.write(answer)
    return {'session': session, 'brief': dsh_board_brief(answer), 'out': args.out}


def _answer_text(row: dict) -> str:
    if 'out' in row:
        return f"{row['brief']}\n(полный ответ: {row['out']})"
    return row.get('answer') or ''


def _board_text(state: dict) -> str:
    goals = state.get('goals', [])
    busy = len(state.get('machines', []))
    lines = [f"доска: {'ПАУЗА' if state.get('paused') else 'работает'}, промптов {len(goals)}, "
             f"занято сессий {busy}"]
    for goal in goals:
        model = goal.get('model') or {}
        chain = f"{model.get('provider')}/{model.get('model')}" if model else 'неположен'
        every = goal.get('everyMinutes') or 0
        period = f'каждые {every} мин' if every else 'разовый'
        text = ' '.join(str(goal.get('text') or '').split())[:100]
        lines.append(f"- {goal.get('id')} [{goal.get('status')}] {period}, {chain}, "
                     f"кругов {goal.get('roundsSpent', 0)}, последний заход "
                     f"{_clock(goal.get('lastRunAt'))}\n    сессия {goal.get('session')}\n    {text}")
        if goal.get('note'):
            lines.append(f"    записка: {goal['note']}")
    return '\n'.join(lines)


def _goal_added_text(value: dict, session: str) -> str:
    mine = [goal for goal in value.get('goals', []) if goal.get('session') == session]
    return f"промпт заведён: {mine[-1].get('id')}" if mine else 'промпт заведён'


if __name__ == '__main__':
    raise SystemExit(dsh_board_cli_main())

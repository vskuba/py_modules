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
PYTHONPATH=. python -m dsh_.dsh_board add <session> "сверяй логи" --every 60
PYTHONPATH=. python -m dsh_.dsh_board limit 4                    # потолок сессий разом
```

`--json` — ответ JSON вместо текста, ставится и до, и после подкоманды.
"""
import argparse
import json
import sys
import time

from dsh_.dsh_api import DSH_API_ADDR_DEFAULT, DshApiError
from dsh_.dsh_board import DSH_BOARD_WAIT_TIMEOUT, DshBoardError, dsh_board_ask, \
    dsh_board_fanout, dsh_board_goal_session, dsh_board_session_new, dsh_board_sessions, \
    dsh_board_settings, dsh_board_state, dsh_board_tool
from dsh_.dsh_session import dsh_session_answer_last, dsh_session_archive, \
    dsh_session_cancel, dsh_session_dialog


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
        answer = dsh_board_ask(session, _text(args.ask), args.wait, args.timeout,
                               not args.no_limit, base=base)
        return _print(args, {'session': session, 'answer': answer},
                      lambda v: f"{v['session']}\n{v['answer']}".rstrip())

    if command == 'ask':
        answer = dsh_board_ask(args.session, _text(args.text), args.wait, args.timeout,
                               not args.no_limit, base=base)
        return _print(args, {'session': args.session, 'answer': answer},
                      lambda v: v['answer'] or 'принято в очередь карточки')

    if command == 'fanout':
        raw = sys.stdin.read() if args.file == '-' else open(args.file, encoding='utf-8').read()
        tasks = json.loads(raw)
        if not isinstance(tasks, list):
            raise ValueError('fanout ждёт JSON-список задач')
        return _print(args, dsh_board_fanout(tasks, args.timeout, base=base), _fanout_text)

    if command == 'answer':
        answer = dsh_session_answer_last(args.session, base=base)
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
            # Без модели цель «неположенная» и не идёт нигде — кладём на цепочку модели
            # своей карточки, как форма делает с черновиком.
            model = dsh_board_state(args.session, base=base).get('sessionModel')
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

    if command == 'limit':
        if args.value < 0:
            raise ValueError('потолок — неотрицательное число; 0 — без потолка')
        dsh_board_settings({'maxParallel': args.value}, base=base)
        return _print(args, {'maxParallel': args.value},
                      lambda v: f"потолок: {v['maxParallel'] or 'нет'}")

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

    command('answer', 'последний ответ карточки').add_argument('session')

    history = command('history', 'промпты и ответы карточки')
    history.add_argument('session')
    history.add_argument('-n', type=int, default=10, help='сколько последних реплик')

    command('cancel', 'прервать идущий круг карточки').add_argument('session')
    command('archive', 'убрать карточку в архив').add_argument('session')

    command('board', 'доска: промпты, статусы, круги, потолок').add_argument(
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
    command('limit', 'потолок сессий, работающих разом').add_argument(
        'value', type=int, help='сколько сессий разом; 0 — без потолка')
    command('pause', 'рубильник: остановить всю работу доски')
    command('resume', 'рубильник: продолжить работу доски')
    return parser


def _wait_args(parser) -> None:
    parser.add_argument('--wait', action='store_true', help='дождаться ответа')
    parser.add_argument('--no-limit', action='store_true',
                        help='не ждать места под потолком хоста (maxParallel)')
    parser.add_argument('--timeout', type=float, default=DSH_BOARD_WAIT_TIMEOUT,
                        help=f'потолок ожидания, с (по умолчанию {DSH_BOARD_WAIT_TIMEOUT})')


def _facet_args(parser) -> None:
    parser.add_argument('--every', type=int, default=None,
                        help='период в минутах; 0 — одноразовый (по умолчанию)')
    parser.add_argument('--quota', type=int, default=None, help='кругов на заход')
    parser.add_argument('--status', choices=['active', 'paused', 'done', 'blocked'], default=None)
    parser.add_argument('--model', default=None, help='цепочка: provider/model')
    parser.add_argument('--motor', type=int, choices=[1, 2], default=None, help='ряд цепочки')
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


def _fanout_text(rows: list) -> str:
    blocks = []
    for index, row in enumerate(rows, 1):
        head = f"#{index} {row['session'] or '—'}"
        blocks.append(f"{head}  ✗ {row['error']}" if row['error'] else f"{head}\n{row['answer']}")
    return '\n\n'.join(blocks)


def _board_text(state: dict) -> str:
    goals = state.get('goals', [])
    limit = state.get('maxParallel') or 0
    busy = len(state.get('machines', []))
    lines = [f"доска: {'ПАУЗА' if state.get('paused') else 'работает'}, промптов {len(goals)}, "
             f"занято сессий {busy}{f' из {limit}' if limit else ' (без потолка)'}"]
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

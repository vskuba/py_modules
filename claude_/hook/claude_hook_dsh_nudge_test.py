"""
Тесты классификации «чтения» и счёта серии чтений.

Транскрипты синтетические — списки записей той формы, которую разбирают
`claude_hook_tool_calls` и `claude_hook_transcript_rows`: живой журнал сюда не нужен,
а живой транскрипт сделал бы тест зависимым от чужой сессии.
"""
import pytest

from claude_.hook.claude_hook_dsh_nudge import claude_hook_dsh_nudge_is_read, \
    claude_hook_dsh_nudge_read_series

# Команды, которые читают: разведка, сделанная руками.
READ_COMMANDS = (
    'cat a.py',
    'grep -rn "def main" .',
    'rg foo',
    'ugrep -w foo README.md',
    'sed -n 1,20p a.py',
    'head -50 log.txt',
    'tail -200 log.txt',
    "find . -name '*.py'",
    'ls -la',
    'wc -l a.py',
    "awk '{print $1}' a.txt",
    'jq .state out.json',
    'tree claude_',
    'stat a.py',
    'file a.bin',
    'diff a.py b.py',
    'git log --oneline -5',
    'git show HEAD',
    'git diff HEAD~1',
    'git grep dsh_mode',
    'git blame claude_/hook/claude_hook.py',
    'git status',
    'git ls-files',
    # составные: чтение не в начале строки — на них старая регулярка и молчала
    'cd /app && grep -rn "pool" .',
    'cd "/tmp/проект" && cat a.md',
    'PYTHONPATH=. grep -rn x .',
    'cat a.json | jq',
    'git fetch -q && ls',
    'git -c core.pager=cat log --oneline',
    'git -C /repo status',
    'cd /srv; ls -1 logs | tail -5',
    # запрос в базу и в журнал — чтение, хоть командой чтения и не выглядит
    'mysql_query sql "select id from jobs limit 10"',
    'PYTHONPATH=. python -m mysql_.mysql_query sql "select 1"',
    'cd /srv && Q() { mysql_query "SELECT 1" > /tmp/a; }; Q "SELECT 1"',
    'opensearch_search "out of memory"',
    'curl -s https://api.example.com/items',
    'curl -H "Accept: application/json" https://x.example/y',
)

# Команды, которые читают не только читают: правка, сборка, выкладка, делегирование.
NOT_READ_COMMANDS = (
    'git commit -m "dsh: застава"',
    'git push origin main',
    'curl -X POST https://api.example.com/items -d \'{"a":1}\'',
    'curl -s -X PUT --data @body.json https://x.example/y',
    'curl -X DELETE https://x.example/y/1',
    'curl -s -d @body.json https://x.example/y',
    "sed -i 's/a/b/' a.py",
    'python -m pytest claude_ -q',
    'rm -rf /tmp/x',
    'echo hi > out.txt',
    'pnpm build',
    # делегирование — не чтение, а ответ на заставу
    'dsh-board new blog "разбор логов" --ask "найди ошибки" --wait --out /tmp/a.md',
    'dsh-board fanout tasks.json --out-dir /tmp/fanout',
    'cd /tmp && dsh-board wait --out-dir /tmp/w',
    'python -m dsh_.dsh_board_cli new blog "имя" --ask "что найти"',
)


def test_read_tools_are_reads():
    """`Read`/`Grep`/`Glob` читают всегда, и в DSH — строчными именами."""
    for name in ('Read', 'Grep', 'Glob', 'read', 'grep', 'glob'):
        assert claude_hook_dsh_nudge_is_read({'name': name, 'input': {}}), name


def test_other_tools_are_not_reads():
    """Правка, запись и чужие инструменты разведкой не считаются."""
    for name in ('Write', 'Edit', 'NotebookEdit', 'edit', 'webfetch', 'TodoWrite', ''):
        assert not claude_hook_dsh_nudge_is_read({'name': name, 'input': {'command': 'cat a.py'}}), name


@pytest.mark.parametrize('command', READ_COMMANDS)
def test_bash_read_commands_are_reads(command):
    """Команда чтения, где бы она в строке ни стояла."""
    assert claude_hook_dsh_nudge_is_read({'name': 'Bash', 'input': {'command': command}}), command


@pytest.mark.parametrize('command', NOT_READ_COMMANDS)
def test_bash_work_commands_are_not_reads(command):
    """Работа — не чтение: правка, сборка, выкладка, зов доски субагентов."""
    assert not claude_hook_dsh_nudge_is_read({'name': 'Bash', 'input': {'command': command}}), command


def test_bash_without_command_is_not_read():
    """Ни команды, ни доводов — не чтение, а сломанная труба: её застава не трогает."""
    assert not claude_hook_dsh_nudge_is_read({'name': 'Bash', 'input': {}})
    assert not claude_hook_dsh_nudge_is_read({'name': 'Bash'})
    assert not claude_hook_dsh_nudge_is_read(None)


def test_series_counts_reads_after_prompt():
    """Серия — чтения после последнего сообщения человека; чужие вызовы в неё не входят."""
    rows = [_prompt('разберись', 'p1'), _result('u1'), _tool('Read', {'file_path': 'a.py'}, 'c1'),
            _tool('Bash', {'command': 'git commit -m x'}, 'c2'), _tool('Grep', {}, 'c3'),
            _result('u2')]

    assert claude_hook_dsh_nudge_read_series(rows) == {'reads': 2, 'start': 'p1'}


def test_series_restarts_after_dsh_board():
    """Один `dsh-board` не глушит счёт до конца хода: после него серия начинается заново."""
    rows = [_prompt('разберись', 'p1'), _tool('Read', {}, 'c1'), _tool('Read', {}, 'c2'),
            _bash('dsh-board new blog "имя" --ask "что найти" --wait --out /tmp/a.md', 'c3'),
            _tool('Read', {}, 'c4')]

    series = claude_hook_dsh_nudge_read_series(rows)

    assert series['reads'] == 1
    assert series['start'] == 'c3'


def test_series_ignores_dsh_board_of_previous_prompt():
    """`dsh-board` прошлого хода — не засечка: новый ход считает с своего сообщения."""
    rows = [_prompt('первый', 'p1'), _bash('dsh-board sessions', 'c1'), _prompt('второй', 'p2'),
            _tool('Read', {}, 'c2'), _tool('Read', {}, 'c3')]

    assert claude_hook_dsh_nudge_read_series(rows) == {'reads': 2, 'start': 'p2'}


def test_series_restarts_after_solo_reason():
    """Строка `Сам:` в ответе агента — обоснование: с неё серия начинается заново."""
    rows = [_prompt('разберись', 'p1')] + [_tool('Read', {}, f'c{i}') for i in range(6)] + \
        [_say('Сам: ищу одну строку, описать задачу дороже.', 'c9'), _tool('Read', {}, 'c10')]

    series = claude_hook_dsh_nudge_read_series(rows)

    assert series['reads'] == 1
    assert series['start'] == 'c9'


def test_solo_line_in_tool_arguments_does_not_reset_series():
    """`Сам:` из аргументов инструмента серию не обнуляет — иначе счёт сбрасывал бы чужой текст."""
    rows = [_prompt('разберись', 'p1')] + [_tool('Read', {}, f'c{i}') for i in range(6)] + \
        [_bash('grep -rn "Сам: " docs/', 'c9')]

    assert claude_hook_dsh_nudge_read_series(rows)['reads'] == 7


def test_dsh_board_call_in_dsh_journal_counts():
    """Журнал DSH: вызов — строка `tool/call` с доводами JSON строкой; он и есть делегирование."""
    rows = [_prompt('разберись', 'p1'), _tool('Read', {}, 'c1'),
            {'type': 'tool/call', 'uuid': 'c2',
             'data': {'name': 'bash', 'arguments': '{"command": "dsh-board new blog \\"имя\\" --ask \\"x\\""}'}},
            _tool('Read', {}, 'c3')]

    series = claude_hook_dsh_nudge_read_series(rows)

    assert series['reads'] == 1
    assert series['start'] == 'c2'


def test_series_without_prompt_counts_whole_tail():
    """Ни засечки в этом хвосте — считается весь хвост, маркер пустой."""
    rows = [_tool('Read', {}, 'c1'), _bash('cat a.py', 'c2')]

    assert claude_hook_dsh_nudge_read_series(rows) == {'reads': 2, 'start': ''}


def test_series_marker_without_uuid_and_time_is_content_hash():
    """Ни номера, ни времени у записи — маркер всё равно есть и он не меняется от сдвига хвоста."""
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': 'разберись'}},
            _tool('Read', {}, 'c1')]
    series = claude_hook_dsh_nudge_read_series(rows)

    assert series['start'].startswith('h')
    assert claude_hook_dsh_nudge_read_series([rows[0]] + rows) == {'reads': 1, 'start': series['start']}


def test_dsh_journal_prompt_and_solo_rows_are_recognized():
    """Формы журнала DSH (`user/message`, `assistant/message`) — те же засечки серии."""
    rows = [{'type': 'user/message', 'uuid': 'p1',
             'data': {'source': {'kind': 'user'}, 'message': {'content': [{'type': 'text', 'text': 'разберись'}]}}},
            _tool('Read', {}, 'c1'),
            {'type': 'assistant/message', 'uuid': 'c2',
             'data': {'message': {'content': [{'type': 'text', 'text': 'Сам: проверю одну строку.'}]}}},
            _tool('Read', {}, 'c3')]

    assert claude_hook_dsh_nudge_read_series(rows) == {'reads': 1, 'start': 'c2'}


def test_dsh_service_message_is_not_a_prompt():
    """Служебная вставка в журнал (не реплика человека) ходом не считается."""
    rows = [_prompt('первый', 'p1'), _tool('Read', {}, 'c1'),
            {'type': 'user/message', 'uuid': 'x1',
             'data': {'source': {'kind': 'system'}, 'message': {'content': [{'type': 'text', 'text': 'context'}]}}}]

    assert claude_hook_dsh_nudge_read_series(rows)['start'] == 'p1'


# --- Построители транскриптов ------------------------------------------------

def _prompt(text: str, uuid: str) -> dict:
    """Сообщение человека."""
    return {'type': 'user', 'uuid': uuid, 'timestamp': f't-{uuid}',
            'message': {'role': 'user', 'content': text}}


def _say(text: str, uuid: str) -> dict:
    """Ответ агента текстом."""
    return {'type': 'assistant', 'uuid': uuid, 'timestamp': f't-{uuid}',
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}


def _result(uuid: str) -> dict:
    """Вывод инструмента — запись человека, но не сообщение человека."""
    return {'type': 'user', 'uuid': uuid,
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]}}


def _tool(name: str, inp: dict, uuid: str) -> dict:
    """Вызов инструмента строкой ассистента."""
    return {'type': 'assistant', 'uuid': uuid, 'timestamp': f't-{uuid}',
            'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': name, 'input': inp}]}}


def _bash(command: str, uuid: str) -> dict:
    """Вызов Bash строкой ассистента."""
    return _tool('Bash', {'command': command}, uuid)

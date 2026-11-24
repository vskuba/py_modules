"""
Тесты заставы «отдай разведку субагенту» (`claude_gate_dsh_delegate`).

Транскрипты синтетические — списки записей той формы, которую разбирают
`claude_hook_tool_calls`; один тест проверяет и чтение журнала с диска. Память заставы
везде указана — в `tmp_path`: настоящая `~/.claude/state/` в тестах не трогается.
"""
import io
import json
import sys

import pytest

from claude_.hook.gate import claude_gate_dsh_delegate as gate
from claude_.hook.gate.claude_gate_dsh_delegate import claude_gate_dsh_delegate_check, \
    claude_gate_dsh_delegate_format

# Режим, в котором застава стоит на пути.
HIGH = 'high'


def test_denies_fifth_read_in_high(tmp_path):
    """Пятое чтение подряд без `dsh-board` в HIGH — отмена с готовой командой."""
    verdict = _check(_rows(4), tmp_path)

    assert verdict['block'] is True
    assert verdict['reads'] == 5

    said = claude_gate_dsh_delegate_format(verdict)

    assert 'dsh-board new' in said and 'fanout' in said and 'Сам:' in said


def test_passes_under_threshold(tmp_path):
    """Пока серия короче порога, застава молчит: чтение нужно, чтобы работать."""
    for reads in range(gate.CLAUDE_GATE_DSH_DELEGATE_READS - 1):
        assert _check(_rows(reads), tmp_path)['block'] is False


def test_repeat_after_denial_passes(tmp_path):
    """Отмена одна на серию: повтор того же вызова проходит, счётчик серии обнулён."""
    assert _check(_rows(4), tmp_path)['block'] is True      # пятое чтение отменено
    assert _check(_rows(5), tmp_path)['block'] is False    # тот же вызов повторяется


def test_next_denial_after_another_series_of_reads(tmp_path):
    """После отмены отсчёт идёт заново: следующая отмена — ещё через порог чтений."""
    assert _check(_rows(4), tmp_path)['block'] is True     # отмена на 5-м

    for reads in range(5, 9):
        assert _check(_rows(reads), tmp_path)['block'] is False, reads

    assert _check(_rows(9), tmp_path)['block'] is True     # 10-е чтение


def test_denial_memory_of_other_series_does_not_hold(tmp_path):
    """`dsh-board` перенёс начало серии — память о прошлой отмене больше не мешает."""
    assert _check(_rows(4), tmp_path)['block'] is True

    rows = _rows(5) + [_bash('dsh-board new blog "разбор" --ask "что найти" --wait --out /tmp/a.md', 'b1'),
                       _tool('Read', 'a.py', 'c20')]

    assert _check(rows, tmp_path)['block'] is False


def test_solo_reason_passes_without_denial(tmp_path):
    """`Сам:` в ответе агента — обоснование: серия с неё, отмены нет."""
    rows = _rows(6) + [_say('Сам: ищу одну строку, описать задачу дороже.', 'z1')]

    assert _check(rows, tmp_path)['block'] is False


def test_solo_reason_after_threshold_of_its_own(tmp_path):
    """После `Сам:` порог считается заново: пять новых чтений — снова отмена."""
    rows = _rows(6) + [_say('Сам: проверю сам.', 'z1')] + [_tool('Read', f'f{i}.py', f'c{i}') for i in range(4)]

    assert _check(rows, tmp_path)['block'] is True


def test_decorated_solo_reason_passes_without_denial(tmp_path):
    """`**Сам:**` с markdown-обёрткой — то же обоснование: серия с неё, отмены нет."""
    rows = _rows(6) + [_say('**Сам:** разбираю конфликт вручную.', 'z1')]

    assert _check(rows, tmp_path)['block'] is False


def test_work_chain_rows_do_not_feed_counter(tmp_path):
    """Цепочки слияния — не чтения: длинная их вереница отмены не даёт."""
    rows = [_prompt('разберись с слиянием', 'p1')] + \
        [_bash('git checkout -q -B t origin/staging && git merge --no-ff --no-commit f '
               '2>&1 | grep -i conflict; git diff --name-only --diff-filter=U', f'b{i}') for i in range(30)]

    assert _check(rows, tmp_path)['block'] is False


def test_heredoc_commit_is_not_a_read(tmp_path):
    """`git commit -F - <<EOF` серию не двигает, даже если в тексте коммита есть `git log`."""
    rows = _rows(2) + [_bash("git commit -q -F - <<'EOF'\nFix\n\ngit log --oneline -3\nEOF", 'b1'),
                       _tool('Read', 'x.py', 'c5')]

    assert _check(rows, tmp_path)['block'] is False     # четвертое чтение — ниже порога


def test_reported_session_denies_fifth_compound_read(tmp_path):
    """Сценарий сессии 16dc3e63: серия составных `cd …; grep "a\\|b"` со вставками посреди
    хода — пятая подряд отменена; ни `|` в кавычках, ни `<task-notification>` счёт не портили."""
    rows = [_prompt('разберись с ошибками', 'p1'),
            _bash('cd /var/www/app; cat -n src/A.php | head -40', 'b1'),
            _inject('<task-notification>\n<task-id>bn4f</task-id>\n</task-notification>', 'n1'),
            _bash('cd /var/www/app; grep -rn "Missing externalId\\|IgnoreException" '
                  '--include=*.php src | head -20', 'b2'),
            _bash('cd /var/www/app; sed -n 1,200p src/B.php', 'b3'),
            _inject('Stop hook feedback:\n[gate]: жди субагентов', 'n2', meta=True),
            _bash('cd /var/www/app; grep -n "json_encode\\|getBody" src/C.php', 'b4')]

    data = {'tool_name': 'Bash', 'tool_input': {'command': 'cd /var/www/app; sed -n 55,140p processors/pay.php'},
            'session_id': 's'}

    verdict = claude_gate_dsh_delegate_check(data, rows=rows, mode=HIGH,
                                             state_path=tmp_path / 'state.json')

    assert verdict['block'] is True
    assert verdict['reads'] == 5


def test_silent_in_free_and_off(tmp_path):
    """Вне HIGH заставы нет: в FREE работает напоминание, в OFF субагентов не зовут."""
    rows = _rows(40)

    for mode in ('free', 'off'):
        assert _check(rows, tmp_path, mode=mode)['block'] is False, mode

    assert not (tmp_path / 'state.json').exists()         # и память не заводится


def test_mode_taken_from_dsh_mode(tmp_path, monkeypatch):
    """Ближайший источник режима — `dsh_mode_get` по `session_id`."""
    monkeypatch.setattr(gate, 'dsh_mode_get', lambda session='': 'free')

    assert claude_gate_dsh_delegate_check(_data('Read'), rows=_rows(40),
                                          state_path=tmp_path / 'state.json')['block'] is False


def test_valve_env_switches_gate_off(tmp_path, monkeypatch):
    """`CLAUDE_GATE_DSH_DELEGATE_PASS=1` снимает заставу целиком, и в HIGH тоже."""
    monkeypatch.setenv('CLAUDE_GATE_DSH_DELEGATE_PASS', '1')

    verdict = _check(_rows(4), tmp_path)

    assert verdict['block'] is False
    assert 'PASS' in verdict['why']


def test_work_command_is_not_denied(tmp_path):
    """Не чтение застава не отменяет: `git commit` в длинной серии — пропуск."""
    data = {'tool_name': 'Bash', 'tool_input': {'command': 'git commit -m "dsh: застава"'},
            'session_id': 's'}

    assert claude_gate_dsh_delegate_check(data, rows=_rows(30), mode=HIGH,
                                         state_path=tmp_path / 'state.json')['block'] is False


def test_delegation_call_is_not_denied(tmp_path):
    """Зов доски — это ответ на заставу, а не чтение: отменять его нельзя."""
    data = {'tool_name': 'Bash', 'tool_input': {'command': 'dsh-board new blog "имя" --ask "x"'},
            'session_id': 's'}

    assert claude_gate_dsh_delegate_check(data, rows=_rows(30), mode=HIGH,
                                         state_path=tmp_path / 'state.json')['block'] is False


def test_without_transcript_gate_is_silent(tmp_path):
    """Ни журнала, ни записей: застава молчит, а не падает и не отменяет работу."""
    data = {'tool_name': 'Read', 'tool_input': {'file_path': 'a.py'}, 'session_id': 's',
            'transcript_path': str(tmp_path / 'нет-такого.jsonl')}

    assert claude_gate_dsh_delegate_check(data, mode=HIGH, state_path=tmp_path / 'state.json')['block'] is False


def test_broken_memory_file_does_not_stop_the_call(tmp_path):
    """Битая память — как пустая: отмена по порогу есть, падения нет."""
    memory = tmp_path / 'state.json'
    memory.write_text('{не json', encoding='utf-8')

    assert _check(_rows(4), tmp_path)['block'] is True


def test_format_says_nothing_when_not_blocking():
    """Нет отмены — нет и текста: пустая причина пропускает вызов."""
    for verdict in ({'block': False, 'reads': 1, 'why': 'режим free'}, {}):
        assert claude_gate_dsh_delegate_format(verdict) == ''


def test_main_prints_pretooluse_deny(tmp_path, monkeypatch, capsys):
    """Хук печатает `permissionDecision: deny` и уходит с нулём — так отказывает `PreToolUse`."""
    journal = tmp_path / 'session.jsonl'
    journal.write_text('\n'.join(json.dumps(one, ensure_ascii=False) for one in _rows(4)) + '\n',
                       encoding='utf-8')
    monkeypatch.setattr(gate, 'dsh_mode_get', lambda session='': HIGH)
    monkeypatch.setattr(gate, 'CLAUDE_GATE_DSH_DELEGATE_STATE', tmp_path / 'state.json')
    monkeypatch.setattr(sys, 'stdin', _stdin({'session_id': 's', 'tool_name': 'Read',
                                             'tool_input': {'file_path': 'a.py'},
                                             'transcript_path': str(journal)}))

    assert gate.main() == 0

    said = json.loads(capsys.readouterr().out)
    output = said['hookSpecificOutput']

    assert output['hookEventName'] == 'PreToolUse'
    assert output['permissionDecision'] == 'deny'
    assert 'dsh-board fanout' in output['permissionDecisionReason']


def test_main_passes_other_tools_silently(tmp_path, monkeypatch, capsys):
    """Инструмент не из списка заставы — ни вывода, ни отмены."""
    monkeypatch.setattr(gate, 'dsh_mode_get', lambda session='': HIGH)
    monkeypatch.setattr(sys, 'stdin', _stdin({'session_id': 's', 'tool_name': 'Edit',
                                             'tool_input': {'file_path': 'a.py'}}))

    assert gate.main() == 0
    assert capsys.readouterr().out == ''


# --- Построители -------------------------------------------------------------

def _data(name: str, inp: dict = None, session: str = 's') -> dict:
    """Ввод хука `PreToolUse`."""
    return {'session_id': session, 'tool_name': name, 'tool_input': inp or {'file_path': 'a.py'}}


def _check(rows: list, tmp_path, mode: str = HIGH, name: str = 'Read') -> dict:
    """Вердикт заставы по синтетическому транскрипту, память — в `tmp_path`."""
    return claude_gate_dsh_delegate_check(_data(name), rows=rows, mode=mode,
                                         state_path=tmp_path / 'state.json')


def _rows(reads: int, session: str = 's') -> list:
    """Ход человека и `reads` чтений подряд; сам отменяемый вызов здесь не записан."""
    return [_prompt('разберись с логами', 'p1')] + [_tool('Read', f'f{i}.py', f'c{i}') for i in range(reads)]


def _stdin(data: dict):
    """Ввод хука как поток."""
    return io.StringIO(json.dumps(data, ensure_ascii=False))


def _prompt(text: str, uuid: str) -> dict:
    return {'type': 'user', 'uuid': uuid, 'timestamp': f't-{uuid}',
            'message': {'role': 'user', 'content': text}}


def _say(text: str, uuid: str) -> dict:
    return {'type': 'assistant', 'uuid': uuid, 'timestamp': f't-{uuid}',
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}


def _tool(name: str, path: str, uuid: str) -> dict:
    inp = {'file_path': path} if name == 'Read' else {}

    return {'type': 'assistant', 'uuid': uuid, 'timestamp': f't-{uuid}',
            'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': name, 'input': inp}]}}


def _bash(command: str, uuid: str) -> dict:
    return {'type': 'assistant', 'uuid': uuid, 'timestamp': f't-{uuid}',
            'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Bash',
                                                          'input': {'command': command}}]}}


def _inject(text: str, uuid: str, meta: bool = False) -> dict:
    """Системная вставка посреди хода: записана харнесом как реплика человека, ходом не является."""
    row = {'type': 'user', 'uuid': uuid, 'timestamp': f't-{uuid}',
           'message': {'role': 'user', 'content': text}}

    return dict(row, isMeta=True) if meta else dict(row, promptSource='system',
                                                    turnOrigin='task_notification',
                                                    origin={'kind': 'task-notification'})

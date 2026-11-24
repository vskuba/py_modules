"""
Тесты заставы «жди живого субагента» (`claude_gate_dsh_alive`).

Ни живого `dsh-board`, ни сети: метки, «входящие» и занятые моторы приходят аргументами
`claude_gate_dsh_alive_check`. Живьём `dsh_wait` читается только в тесте чужого владельца —
его файл меток вывернут в `tmp_path` переменной `DSH_HOME`.
"""
import io
import json
import sys
import time

from claude_.hook.gate import claude_gate_dsh_alive
from claude_.hook.gate.claude_gate_dsh_alive import claude_gate_dsh_alive_check, claude_gate_dsh_alive_format

CARD = 'a1b2c3d4-1111-2222-3333-444444444444'
OWNER = 'owner-session-0001'
WORKING = [{'session': CARD, 'title': 'разбор логов'}]
ANSWERED = [{'session': CARD, 'title': 'разбор логов', 'turns': 2, 'brief': 'готово'}]


def test_no_marks_passes():
    """Сессия никого не ждёт — застава молчит: правило только про ожидание ответа."""
    assert not _check(file_path='x.py', marks={})['block']


def test_working_card_denies_read():
    """Карточка занята — чтение кода отменено с готовым текстом про фонового ждуна."""
    verdict = _check(file_path='deepseek/hook/x.py', working=WORKING)

    assert verdict['block'] is True and verdict['mode'] == 'wait'

    said = claude_gate_dsh_alive_format(verdict)

    assert 'run_in_background' in said and 'dsh-board wait' in said and 'разбор логов' in said
    assert 'сторож' in said and 'перестанет работать' in said


def test_working_card_denies_bash_cat():
    """`cat` — по общему правилу чтение; живой субагент отменяет и его."""
    assert _check('Bash', command='cat x.py', working=WORKING)['block'] is True


def test_working_card_denies_every_call():
    """Отказа «один на серию» нет: чтение не запрещено, а отложено — помнит ответ, не застава."""
    for _ in range(3):
        assert _check(file_path='x.py', working=WORKING)['block'] is True


def test_read_answer_file_passes():
    """Ответ субагента из scratch-каталога ждун кладёт в /tmp — читать его можно."""
    assert not _check(file_path='/tmp/dsh-wait-ab12cd34/resp.md', working=WORKING)['block']


def test_grep_memory_passes(monkeypatch):
    """Память и настройки Claude, метки DSH — свои файлы, не разведка кода."""
    monkeypatch.setenv('HOME', '/home/tester')

    assert not _check('Grep', path='/home/tester/.claude/projects/x.jsonl', working=WORKING)['block']
    assert not _check(file_path='/home/tester/.dsh/dsh-board.expected.json', working=WORKING)['block']


def test_glob_tmp_passes():
    """`Glob` по /tmp — тоже свои файлы."""
    assert not _check('Glob', path='/tmp', pattern='*.md', working=WORKING)['block']


def test_bash_dsh_board_passes():
    """Вызов `dsh-board` — делегирование, а не чтение: застава его не трогает вовсе."""
    assert not _check('Bash', command='dsh-board inbox', working=WORKING)['block']


def test_bash_answer_file_passes():
    """Bash-чтение, у которого все пути — в /tmp, проходит."""
    assert not _check('Bash', command='grep -n итог /tmp/dsh-wait-ab12cd34/resp.md', working=WORKING)['block']


def test_bash_relative_file_denies():
    """Без путей команда читает из рабочего каталога; относительный файл вперемешку — тоже."""
    assert not _check('Bash', command='head -n 5 /tmp/a.md', working=WORKING)['block']
    assert _check('Bash', command='cat /tmp/a.md x.py', working=WORKING)['block'] is True


def test_other_owner_passes(tmp_path, monkeypatch):
    """Метка чужой сессии — не наши ожидания: живьём, через метки в `DSH_HOME` = tmp_path."""
    monkeypatch.setenv('DSH_HOME', str(tmp_path))
    from dsh_.dsh_wait import dsh_wait_expect
    dsh_wait_expect(CARD, 'chuzhaya-sessiya')
    verdict = claude_gate_dsh_alive_check(_data(file_path='x.py'), answered=[], working=WORKING)

    assert not verdict['block'] and 'не ждёт' in verdict['why']


def test_answer_ready_denies_to_take():
    """Ответ пришёл и не забран — отмена не «жди», а «забери его: dsh-board wait / collect / inbox»."""
    verdict = _check(file_path='x.py', answered=ANSWERED)

    assert verdict['block'] is True and verdict['mode'] == 'take'

    assert 'забери' in claude_gate_dsh_alive_format(verdict)


def test_dead_card_passes():
    """Ни ответа, ни признаков жизни: ни занятого мотора, ни running — действуй сама."""
    assert not _check(file_path='x.py', answered=[], working=[])['block']


def test_valve_passes(monkeypatch):
    """Клапан снимает заставу целиком, как и у заставы делегирования."""
    monkeypatch.setenv('CLAUDE_GATE_DSH_ALIVE_PASS', '1')

    assert not _check(file_path='x.py', working=WORKING)['block']


def test_main_denies_read(monkeypatch, capsys):
    """Хук оформляет отказ по договору `PreToolUse` и молчит кодом возврата."""
    monkeypatch.setattr(claude_gate_dsh_alive, 'dsh_wait_marks', lambda owner: _marks())
    monkeypatch.setattr(claude_gate_dsh_alive, 'dsh_wait_answered', lambda sessions, **kw: [])
    monkeypatch.setattr(claude_gate_dsh_alive, '_working', lambda keys: WORKING)
    monkeypatch.setattr(sys, 'stdin', _stdin(_data(file_path='x.py')))

    assert claude_gate_dsh_alive.main() == 0
    assert json.loads(capsys.readouterr().out)['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_broken_internals_passes(monkeypatch, capsys):
    """Своя поломка пропускает вызов с заметкой в stderr: застава — не стена."""
    def boom(*args, **kwargs):
        raise RuntimeError('нет хоста')

    monkeypatch.setattr(claude_gate_dsh_alive, 'dsh_wait_marks', boom)
    monkeypatch.setattr(sys, 'stdin', _stdin(_data(file_path='x.py')))

    assert claude_gate_dsh_alive.main() == 0
    captured = capsys.readouterr()

    assert 'пропущен' in captured.err and not captured.out


# --- Приватные: ниже всех публичных (code_rules.md, §3) -----------------------

def _marks(seconds: float = 300.0, owner: str = OWNER) -> dict:
    return {CARD: {'owner': owner, 'at': time.time() - seconds}}


def _data(name: str = 'Read', **tool_input) -> dict:
    return {'session_id': OWNER, 'tool_name': name, 'tool_input': tool_input}


def _check(name: str = 'Read', marks=None, answered=(), working=(), **tool_input) -> dict:
    return claude_gate_dsh_alive_check(_data(name, **tool_input), marks=_marks() if marks is None else marks,
                                       answered=list(answered), working=list(working))


def _stdin(data: dict):
    """Ввод хука как поток."""
    return io.StringIO(json.dumps(data, ensure_ascii=False))

"""
Тесты хука перестановки ждуна: виденное не всплывает, мёртвый ждун — с командой, живой — тихо.

`DSH_HOME` и `TMPDIR` — в `tmp_path`; `~/.dsh` и `/tmp` не трогаем. Метки, живой ли ждун,
и «входящие» хоста подменены — хост для теста не нужен вовсе.
"""
import io
import json
import os
import time

import pytest

from claude_.hook import claude_hook_dsh_rearm as hook

SID = 'e1b5b2cc-09f8-40e2-9295-fd8893da7643'
CARD = 'continuous-aaaa1111-2222-3333-4444-555566667777'


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """Каталоги сессии в tmp и подмена трёх признаков (`pending`, `alive`, `answered`)."""
    home, tmp = tmp_path / 'dsh', tmp_path / 'tmp'
    home.mkdir()
    tmp.mkdir()
    monkeypatch.setenv('DSH_HOME', str(home))
    monkeypatch.setenv('TMPDIR', str(tmp))
    monkeypatch.delenv('CLAUDE_CODE_SESSION_ID', raising=False)
    out = {'tmp': tmp, 'home': home, 'answered_calls': []}

    def arm(pending=(), alive=False, answered=(), error=None):
        monkeypatch.setattr(hook, 'dsh_wait_pending', lambda owner: sorted(pending))
        monkeypatch.setattr(hook, 'dsh_wait_waiter_alive', lambda owner: alive)

        def _answered(sessions):
            out['answered_calls'].append(list(sessions))
            if error:
                raise error
            return list(answered)

        monkeypatch.setattr(hook, 'dsh_wait_answered', _answered)

    def answer_file(card=CARD, mtime=None, where=None):
        seat = where or (tmp / f'dsh-wait-{SID[:8]}')
        seat.mkdir(parents=True, exist_ok=True)
        path = seat / f'{card}.md'
        path.write_text('answer body', encoding='utf-8')
        stamp = mtime or time.time()
        os.utime(path, (stamp, stamp))
        return str(path)

    out['arm'] = arm
    out['file'] = answer_file
    return out


def data(**extra):
    return {'session_id': SID, 'hook_event_name': 'SessionStart', **extra}


def test_quiet_when_nothing_to_say(stage):
    """Ни меток, ни unseen-файлов — хук молчит и state-файл не создаёт."""
    stage['arm']()
    assert hook.claude_hook_dsh_rearm_context(data()) == ''
    assert not (stage['home'] / hook.CLAUDE_HOOK_DSH_REARM_STATE).exists()


def test_collected_answer_reported_once_then_silent(stage):
    """Собранный ждуном файл — один раз; тот же mtime больше не всплывает, новый — снова."""
    stage['arm'](alive=True)
    path = stage['file']()

    text = hook.claude_hook_dsh_rearm_context(data())
    assert '1 answer(s) ready' in text and path in text
    assert hook.claude_hook_dsh_rearm_context(data()) == ''

    os.utime(path, (time.time() + 10, time.time() + 10))       # ждун перезаписал файл ответом
    assert path in hook.claude_hook_dsh_rearm_context(data())


def test_collected_answers_capped_with_count(stage):
    """Больше `CLAUDE_HOOK_DSH_REARM_SHOWN` — первые N путями, остальные числом."""
    stage['arm'](alive=True)
    for n in range(7):
        stage['file'](card=f'continuous-card{n:04d}')

    text = hook.claude_hook_dsh_rearm_context(data())
    assert '7 answer(s) ready' in text and '(+2 more in' in text


def test_old_answers_not_resurfaced(stage):
    """Файл старше `DSH_WAIT_MAX_AGE` — как просроченная метка: мимо отчёта."""
    stage['arm'](alive=True)
    stage['file'](mtime=time.time() - hook.DSH_WAIT_MAX_AGE - 60)
    assert hook.claude_hook_dsh_rearm_context(data()) == ''


def test_dead_waiter_rearm_command_with_running_names(stage):
    """Метки есть, ждуна нет — отчёт с именами карточек и готовой командой ждуна."""
    stage['arm'](pending=['continuous-baba9279-1', 'continuous-59369515-2'], answered=[])

    text = hook.claude_hook_dsh_rearm_context(data())
    # `dsh_wait_pending` отдаёт метки отсортированными — тот же порядок и в именах.
    assert '2 subagent(s) still running: 59369515, baba9279' in text
    assert f'cd /tmp && dsh-board wait --out-dir {os.environ["TMPDIR"]}/dsh-wait-{SID[:8]}' in text
    assert 'run_in_background' in text


def test_alive_waiter_makes_no_host_call(stage):
    """Ждун жив — будить некого и «входящие» спрашивать нечего: тихо, без вызова хосту."""
    stage['arm'](pending=['continuous-baba9279-1'], alive=True)
    assert hook.claude_hook_dsh_rearm_context(data()) == ''
    assert stage['answered_calls'] == []


def test_answered_inbox_separated_from_running(stage):
    """Ответ легли, метка стоит — одна строка «в входящих», остальные — «ещё работают»."""
    answered = [{'session': 'continuous-baba9279-1', 'title': 'log analysis', 'turns': 3, 'brief': 'x'}]
    stage['arm'](pending=['continuous-baba9279-1', 'continuous-59369515-2'], answered=answered)

    text = hook.claude_hook_dsh_rearm_context(data())
    assert '1 answer(s) sit in the inbox, never collected: "log analysis"' in text
    assert '1 subagent(s) still running: 59369515' in text
    assert 'baba9279' not in text.split('still running')[1]


def test_answer_file_and_inbox_row_mentioned_once(stage):
    """Ждун умер между записью файла и забором: карточка — в «готовых», не в «входящих»."""
    path = stage['file'](card='continuous-baba9279-1')
    answered = [{'session': 'continuous-baba9279-1-g2', 'title': 'log analysis', 'turns': 3, 'brief': 'x'}]
    stage['arm'](pending=['continuous-baba9279-1'], answered=answered)

    text = hook.claude_hook_dsh_rearm_context(data())
    assert f'"log analysis" -> {path}' in text
    assert 'sit in the inbox' not in text
    assert 'still running' not in text
    # метка снялась бы забором — ждун всё равно нужен только если что-то осталось; здесь пусто
    assert 'dsh-board wait --out-dir' in text


def test_host_error_degrades_to_local_report(stage):
    """Хост недоступен — отчёт по локальным признакам, без «входящих», без падения."""
    stage['arm'](pending=['continuous-baba9279-1'], error=OSError('host down'))
    text = hook.claude_hook_dsh_rearm_context(data())
    assert '1 subagent(s) still running: baba9279' in text


def test_host_hang_capped_by_deadline(stage, monkeypatch):
    """Зависший хост не держит старт: поток с потолком, отчёт приходит по локальным меткам."""
    monkeypatch.setattr(hook, 'CLAUDE_HOOK_DSH_REARM_HOST_WAIT', 0.2)
    stage['arm'](pending=['continuous-baba9279-1'])

    def hang(sessions):                       # «хост» спит дольше потолка
        time.sleep(3)
        return []

    monkeypatch.setattr(hook, 'dsh_wait_answered', hang)
    started = time.monotonic()
    text = hook.claude_hook_dsh_rearm_context(data())
    assert time.monotonic() - started < 1.5
    assert '1 subagent(s) still running: baba9279' in text


def test_state_prunes_entries_out_of_window(stage):
    """Записи старше окна (файл всё равно не будет показан) из state уходят, а не копятся."""
    stage['arm']()
    state = stage['home'] / hook.CLAUDE_HOOK_DSH_REARM_STATE
    state.write_text(json.dumps({'/tmp/gone.md': time.time() - hook.DSH_WAIT_MAX_AGE - 5,
                                 '/tmp/kept.md': time.time() - 60}), encoding='utf-8')

    hook._state_write({'/tmp/new.md': time.time()})
    rows = json.loads(state.read_text(encoding='utf-8'))
    assert '/tmp/gone.md' not in rows and '/tmp/kept.md' in rows and '/tmp/new.md' in rows


def test_no_session_id_is_silent(stage):
    """Ни `session_id`, ни `CLAUDE_CODE_SESSION_ID` — нечего искать, молчим."""
    stage['arm'](pending=['continuous-baba9279-1'])
    assert hook.claude_hook_dsh_rearm_context({'hook_event_name': 'SessionStart'}) == ''


def test_main_prints_json_and_echoes_event(stage, capsys, monkeypatch):
    """stdout — `additionalContext` с именем события из ввода; тишина — пустой stdout."""
    stage['arm'](pending=['continuous-baba9279-1'])
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(
        {'session_id': SID, 'hook_event_name': 'UserPromptSubmit'})))

    assert hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
    assert 'dsh-board wait --out-dir' in out['hookSpecificOutput']['additionalContext']

    monkeypatch.setattr(hook, 'dsh_wait_pending', lambda owner: [])
    assert hook.main() == 0
    assert capsys.readouterr().out == ''


def test_main_swallows_its_own_breakage(stage, capsys, monkeypatch):
    """Своя поломка — молчание и строчка в stderr, а не падение старта сессии."""
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'session_id': SID})))

    def explode(owner):
        raise RuntimeError('bug')

    monkeypatch.setattr(hook, 'dsh_wait_pending', explode)
    assert hook.main() == 0
    captured = capsys.readouterr()
    assert captured.out == '' and 'claude_hook_dsh_rearm' in captured.err

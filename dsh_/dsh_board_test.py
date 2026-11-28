"""Тесты слоя `dsh_board`: worktree-workspace карточки и раздачу задач через бокс доски.

Карточка `--worktree` не плодит на панели новый проект: хост не принимает cwd отдельно
от workspace (см. DSH_BOARD_WORKTREE_WORKSPACE), и копия доносится карточке метаданными
и шапкой промпта. Задачи с ожиданием идут через бокс доски: `continuous_add` от имени
карточки, ответ — круг цели, цель снята после ответа. Сеть не трогается:
`dsh_api_post`/`dsh_api_get`/`dsh_api_call` и git подменены; `DSH_HOME` — в `tmp_path`,
чтобы не писать в `~/.dsh`.
"""
import base64
import json
import os

import pytest

from dsh_ import dsh_board, dsh_board_cli, dsh_session
from dsh_.dsh_board_cli import dsh_board_cli_main

COPY = '/repo/.claude/worktrees/dsh-pravka'      # копия репо: с приметой MARK
COPY_OLD = '/repo/.claude/worktrees/dsh-staraya' # копия старой карточки — только в cwd


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Хост, git и файлы слоя — подставные; `DSH_HOME` — в tmp, `~/.dsh` не трогаем."""
    home = tmp_path / 'dsh-home'
    monkeypatch.setenv('DSH_HOME', str(home))
    posts, calls = [], []

    def post(route, body, **_kw):
        posts.append((route, body))
        return {'session': 'card-1'}

    def get(route, **_kw):
        return {'workspaces': box.state['workspaces'], 'robots': box.state['robots'],
                'archived': box.state['archived']}

    def call(route, body, **_kw):
        calls.append((route, body))
        return {}

    monkeypatch.setattr(dsh_board, 'dsh_api_post', post)
    monkeypatch.setattr(dsh_board, 'dsh_api_get', get)
    monkeypatch.setattr(dsh_board, 'dsh_api_call', call)
    monkeypatch.setattr(dsh_board, 'dsh_saving_owner_mark', lambda session: None)
    box = _Sandbox()
    box.tmp, box.home = tmp_path, home
    box.technical = home / 'dsh-worktrees'
    box.meta = home / 'dsh-board.worktrees.json'
    box.state = {'workspaces': [], 'robots': [], 'archived': []}
    box.posts, box.calls = posts, calls
    return box


class _Sandbox:
    """Набор подмен фикстуры (Path не даёт вешать на себя атрибуты)."""


# ── заведение карточки ──

def test_new_worktree_card_lives_in_technical_workspace(sandbox):
    """Карточка копии встаёт в общий технический workspace, а копия — в метаданные."""
    session = dsh_board.dsh_board_session_new(COPY, 'правка', worktree=_tree())
    assert session == 'card-1'
    route, body = sandbox.posts[0]
    assert route == 'continuous-agent.session'
    assert body['workspace'] == str(sandbox.technical)      # не путь копии
    assert os.path.isfile(sandbox.technical / 'README.md')  # папка создана, это не репозиторий
    assert os.listdir(sandbox.technical) == ['README.md']
    assert json.loads(sandbox.meta.read_text())['card-1']['path'] == COPY
    assert json.loads(sandbox.meta.read_text())['card-1']['branch'] == 'dsh/pravka'


def test_session_new_without_worktree_keeps_cwd(sandbox):
    """Обычная карточка — как раньше: workspace = её каталог, метаданных нет."""
    dsh_board.dsh_board_session_new('/some/project', 'обычная')
    assert sandbox.posts[0][1]['workspace'] == '/some/project'
    assert not sandbox.meta.exists()


def test_cli_new_worktree_prompt_head_and_metadata(sandbox, monkeypatch, capsys):
    """CLI `new --worktree --ask`: шапка с путём копии — в начале промпта, копия — в метаданных."""
    asked = []
    monkeypatch.setattr(dsh_board_cli, 'dsh_worktree_new', lambda repo, name: _tree())
    monkeypatch.setattr(dsh_board_cli, 'dsh_board_ask',
                        lambda session, text, *a, **kw: asked.append(text) or 'ok')
    dsh_board_cli_main(['new', '/repo', 'правка', '--worktree', '--ask', 'починь баг', '--json'])
    capsys.readouterr()
    assert asked and asked[0].startswith('Your repository copy: ' + COPY)
    assert 'cd ' + COPY in asked[0] and 'do not touch the main repository /repo' in asked[0]
    assert json.loads(sandbox.meta.read_text())['card-1']['path'] == COPY


# ── поиск копии карточкой ──

def test_worktree_row_reads_metadata_by_generation_key(sandbox):
    """Метаданные — по ключу карточки: поколение `-gN` не ломает поиск."""
    dsh_board.dsh_board_session_new(COPY, 'правка', worktree=_tree())
    assert dsh_board.dsh_board_worktree_row('card-1-g3')['path'] == COPY
    assert dsh_board.dsh_board_worktree_row('чужая') == {}


def test_cli_land_finds_copy_by_metadata(sandbox, monkeypatch, capsys):
    """`land` новой карточки берёт копию из метаданных, а не из cwd (там технический workspace)."""
    landed = []
    monkeypatch.setattr(dsh_board_cli, 'dsh_session_find',
                        lambda session, **kw: {'cwd': str(sandbox.technical)})
    monkeypatch.setattr(dsh_board_cli, 'dsh_board_archive', lambda session, **kw: {})
    monkeypatch.setattr(dsh_board_cli, 'dsh_worktree_land',
                        lambda path, message: landed.append(path) or {'branch': 'dsh/pravka', 'merged': True})
    dsh_board.dsh_board_session_new(COPY, 'правка', worktree=_tree())
    sandbox.state['workspaces'] = [{'id': 'w-tech', 'path': str(sandbox.technical)}]
    dsh_board_cli_main(['land', 'card-1', '--json'])
    capsys.readouterr()
    assert landed == [COPY]
    # технический workspace не снимается, и метаданные стёрты; копии-проекта на панели нет.
    assert sandbox.calls == []
    assert dsh_board.dsh_board_worktree_row('card-1') == {}


def test_cli_land_old_card_falls_back_to_cwd(sandbox, monkeypatch, capsys):
    """Старая карточка (метаданных нет) — копия по-прежнему в её cwd, проект снимается."""
    landed = []
    monkeypatch.setattr(dsh_board_cli, 'dsh_session_find', lambda session, **kw: {'cwd': COPY_OLD})
    monkeypatch.setattr(dsh_board_cli, 'dsh_board_archive', lambda session, **kw: {})
    monkeypatch.setattr(dsh_board_cli, 'dsh_worktree_land',
                        lambda path, message: landed.append(path) or {'branch': 'dsh/staraya', 'merged': True})
    sandbox.state['workspaces'] = [{'id': 'w-old', 'path': COPY_OLD}]
    dsh_board_cli_main(['land', 'card-1', '--json'])
    capsys.readouterr()
    assert landed == [COPY_OLD]
    assert sandbox.calls == [('workspace/delete', {'request': {'workspaceId': 'w-old'}})]


# ── уборка ──

def test_gc_keeps_technical_workspace_and_sweeps_old_copies(sandbox, monkeypatch):
    """gc: технический workspace не трогает, старые копии и их проекты — убирает как раньше."""
    archived, dropped = [], []
    # технического workspace на панели и каталога его нет вовсе — всё равно не снимается.
    sandbox.state['workspaces'] = [{'id': 'w-tech', 'path': str(sandbox.technical)}]
    new_copy = sandbox.tmp / 'gone-copy'      # копия новой карточки — каталога нет
    old_copy = sandbox.tmp / 'repo' / '.claude' / 'worktrees' / 'dsh-staraya'
    old_copy.mkdir(parents=True)              # старая копия — по cwd карточки
    sandbox.state['workspaces'].append({'id': 'w-old', 'path': str(old_copy)})
    monkeypatch.setattr(dsh_board, 'dsh_session_list', lambda **kw: [
        {'sessionId': 'card-new', 'cwd': str(sandbox.technical), 'updatedAt': 0},
        {'sessionId': 'card-old', 'cwd': str(old_copy), 'updatedAt': 0},
    ])
    monkeypatch.setattr(dsh_board, 'dsh_session_archive', lambda session, **kw: archived.append(session))
    monkeypatch.setattr(dsh_board, 'dsh_worktree_drop', lambda path, **kw: dropped.append(path))
    monkeypatch.setattr(dsh_board, 'dsh_worktree_dirty', lambda path: False)
    monkeypatch.setattr(dsh_board, 'dsh_worktree_merged', lambda path: True)
    _dsh_json(sandbox.meta, {'card-new': {'path': str(new_copy), 'branch': 'dsh/gone', 'root': '/repo'}})
    sandbox.state['robots'] = ['card-new', 'card-old']
    rows = dsh_board.dsh_board_gc(base='x:1')
    assert {r['session'] for r in rows} == {'card-new', 'card-old'}
    assert set(archived) == {'card-new', 'card-old'}
    assert dropped == [str(old_copy)]                # старую копию снять, новой уже нет
    assert sandbox.calls == [('workspace/delete', {'request': {'workspaceId': 'w-old'}})]
    assert dsh_board.dsh_board_worktree_row('card-new') == {}   # метаданные мёртвой карточки стёрты


def test_gc_drops_emptied_workspace_keeps_human(sandbox, monkeypatch):
    """gc: проект, где все карточки агентские и в архиве, снимается; с карточкой человека — нет."""
    sandbox.state['workspaces'] = [{'id': 'w-bot', 'path': '/proj/bot'},
                                   {'id': 'w-mix', 'path': '/proj/mix'},
                                   {'id': 'w-new', 'path': '/proj/new'}]
    monkeypatch.setattr(dsh_board, 'dsh_session_list', lambda **kw: [
        {'sessionId': 'bot-old', 'cwd': '/proj/bot', 'updatedAt': 0},
        {'sessionId': 'bot-done', 'cwd': '/proj/bot', 'updatedAt': 0},
        {'sessionId': 'bot-mix', 'cwd': '/proj/mix', 'updatedAt': 0},
        {'sessionId': 'human', 'cwd': '/proj/mix', 'updatedAt': 0},
    ])
    monkeypatch.setattr(dsh_board, 'dsh_session_archive', lambda session, **kw: None)
    sandbox.state['robots'] = ['bot-old', 'bot-done', 'bot-mix']
    sandbox.state['archived'] = ['bot-done']
    dsh_board.dsh_board_gc(base='x:1')
    assert sandbox.calls == [('workspace/delete', {'request': {'workspaceId': 'w-bot'}})]


def test_workspace_drop_refuses_technical(sandbox):
    """Снять технический workspace руками — отказ, его файлы и карточки общие."""
    sandbox.state['workspaces'] = [{'id': 'w-tech', 'path': str(sandbox.technical)}]
    assert dsh_board.dsh_board_workspace_drop(str(sandbox.technical), base='x:1') is False
    assert sandbox.calls == []


# ── промпт проверяющего ──

def test_verify_of_worktree_card_gets_head(sandbox, monkeypatch):
    """Карточка-проверяющий копии — тоже в техническом workspace, с шапкой о пути копии."""
    monkeypatch.setattr(dsh_board, 'dsh_session_find',
                        lambda session, **kw: {'cwd': str(sandbox.technical), 'projections': {}})
    asked = []

    monkeypatch.setattr(dsh_board, 'dsh_session_round_pair',
                        lambda session, **kw: {'task': 'задача', 'answer': 'ответ', 'via': 'user'})
    monkeypatch.setattr(dsh_board, 'dsh_session_title', lambda item: 'правка')
    monkeypatch.setattr(dsh_board, 'dsh_board_ask',
                        lambda session, text, *a, **kw: asked.append(text) or 'вердикт')
    _dsh_json(sandbox.meta, {'card-1': {'path': COPY, 'branch': 'dsh/pravka', 'root': '/repo'}})
    dsh_board.dsh_board_verify('card-1', base='x:1')
    assert asked[0].startswith('Your repository copy: ' + COPY)
    assert sandbox.posts[0][1]['workspace'] == str(sandbox.technical)


# ── задача через бокс доски ──

MODEL = {'provider': 'deepseek', 'model': 'dsh-test'}


def test_ask_via_box_adds_in_card_name_and_drops_after_answer(sandbox, monkeypatch):
    """`ask --wait` идёт через бокс: засечка до клада, `continuous_add` именем карточки,
    цель снята после ответа."""
    events = _box(monkeypatch, sandbox, {'починь баг': 'готово'})
    answer = dsh_board.dsh_board_ask('card-1', 'починь баг', wait=True, timeout=5)
    assert answer == 'готово'
    adds = [e for e in events if e[0] == 'tool' and e[2] == 'continuous_add']
    assert len(adds) == 1
    _, session, _, args = adds[0]
    assert session == 'card-1'                                   # в собственном имени карточки
    assert args['text'] == 'починь баг' and args['model'] == MODEL
    assert args['everyMinutes'] == 0 and args['quotaRounds'] == 1
    # Засечка журнала — до клада: круг стартует через секунду после добавления, а ответ
    # ищется от засечки, а не от клада.
    assert min(i for i, e in enumerate(events) if e[0] == 'cursor') < next(
        i for i, e in enumerate(events) if e[0] == 'tool' and e[2] == 'continuous_add')
    drops = [e for e in events if e[0] == 'tool' and e[2] == 'continuous_drop']
    assert drops and drops[0][1] == 'card-1' and drops[0][3] == {'goal': 'goal-1'}


def test_ask_via_box_keeps_goal_when_answer_not_awaited(sandbox, monkeypatch):
    """Не дождались ответа — цель на доске остаётся: живой круг добивает человек, не мы."""
    events = _box(monkeypatch, sandbox, {})                     # круг не кончится вовсе
    with pytest.raises(dsh_board.DshBoardError):
        dsh_board.dsh_board_ask('card-1', 'долгая работа', wait=True, timeout=0.05)
    assert [e for e in events if e[0] == 'tool' and e[2] == 'continuous_add']
    assert not [e for e in events if e[0] == 'tool' and e[2] == 'continuous_drop']


def test_ask_without_wait_goes_to_box_not_direct_prompt(sandbox, monkeypatch):
    """`ask` без `--wait` — тоже клад в бокс: прямой промпт встал бы мотором «руками»
    сверх числа моторов (24.11 — пятый мотор при четырёх)."""
    events = _box(monkeypatch, sandbox, {})
    assert dsh_board.dsh_board_ask('card-1', 'разведка', wait=False, timeout=5) == ''
    adds = [e for e in events if e[0] == 'tool' and e[2] == 'continuous_add']
    assert len(adds) == 1 and adds[0][3]['text'] == 'разведка'
    assert not sandbox.calls                                    # прямого промпта нет
    assert not [e for e in events if e[0] == 'wait']            # ответа не ждём


def test_box_clear_drops_only_done_goals_of_card(sandbox, monkeypatch):
    """Уборка после `dsh-board wait`: снимаются закрытые цели этой карточки, и только они."""
    goals = [{'id': 'g1', 'session': 'card-1', 'status': 'done'},
             {'id': 'g2', 'session': 'card-1-g2', 'status': 'done'},
             {'id': 'g3', 'session': 'card-1', 'status': 'active'},
             {'id': 'g4', 'session': 'card-2', 'status': 'done'}]
    monkeypatch.setattr(dsh_board, 'dsh_api_get', lambda route, **_kw: {'goals': goals})
    assert dsh_board.dsh_board_box_clear('card-1') == ['g1', 'g2']
    drops = [body['args'] for route, body in sandbox.posts if route == 'continuous-agent.tool']
    assert drops == [{'goal': 'g1'}, {'goal': 'g2'}]


def test_ask_box_resumes_interrupted_round_with_direct_prompt(sandbox, monkeypatch):
    """Оборванный круг продолжается прямым промптом — как раньше; цель — снята после ответа."""
    events = _box(monkeypatch, sandbox, {})
    probes = {'n': 0}

    def turn_reply(session, mark, asked, timeout, **_kw):
        events.append(('wait', session, asked, mark))
        probes['n'] += 1
        if probes['n'] == 1:
            return {'text': 'искал…', 'reason': 'interrupted', 'seq': 20}
        return {'text': 'готово', 'reason': 'completed', 'seq': 30}

    monkeypatch.setattr(dsh_board, 'dsh_session_turn_reply', turn_reply)
    answer = dsh_board.dsh_board_ask('card-1', 'задача', wait=True, timeout=5)
    assert answer == 'готово'
    prompts = [c for c in sandbox.calls if c[0] == 'session/prompt']   # продолжение — прямое
    assert prompts and prompts[0][1]['request']['content'][0]['text'] == dsh_board.DSH_BOARD_RESUME_TEXT
    assert [e for e in events if e[0] == 'tool' and e[2] == 'continuous_drop']


def test_ask_via_box_carries_images_as_base64(sandbox, monkeypatch, tmp_path):
    """Картинки едут целью: `{mediaType, data: base64, name}` — круг положит их в промпт."""
    shot = tmp_path / 'shot.png'
    shot.write_bytes(b'\x89PNG fake')
    events = _box(monkeypatch, sandbox, {'глянь скриншот': 'вот оно'})
    dsh_board.dsh_board_ask('card-1', 'глянь скриншот', wait=True, timeout=5, images=[str(shot)])
    args = next(e[3] for e in events if e[0] == 'tool' and e[2] == 'continuous_add')
    image = args['images'][0]
    assert image['mediaType'] == 'image/png' and image['name'] == 'shot.png'
    assert base64.b64decode(image['data']) == shot.read_bytes()


def test_fanout_places_in_box_order_and_waits_afterward(sandbox, monkeypatch):
    """`fanout`: клады по порядку задач, ждут — уже после них; цели сняты после ответов."""
    events = _box(monkeypatch, sandbox, {'первая': 'ответ-1', 'вторая': 'ответ-2'})
    rows = dsh_board.dsh_board_fanout([{'cwd': '/p/1', 'text': 'первая'},
                                       {'cwd': '/p/2', 'text': 'вторая'}], timeout=5)
    assert [r['answer'] for r in rows] == ['ответ-1', 'ответ-2']
    assert not any(r['error'] for r in rows)
    order = [e[3]['text'] for e in events if e[0] == 'tool' and e[2] == 'continuous_add']
    assert order == ['первая', 'вторая']                       # порядок бокса — порядок задач
    last_add = max(i for i, e in enumerate(events) if e[0] == 'tool' and e[2] == 'continuous_add')
    first_wait = min(i for i, e in enumerate(events) if e[0] == 'wait')
    assert first_wait > last_add                               # нити ждут, а не заводят
    drops = sorted(e[3]['goal'] for e in events if e[0] == 'tool' and e[2] == 'continuous_drop')
    assert drops == ['goal-1', 'goal-2']


def test_fanout_error_task_keeps_goal_and_others_answer(sandbox, monkeypatch):
    """Сорвавшаяся задача — своей целью не рискует, других не роняет: ответ у каждой свой."""
    events = _box(monkeypatch, sandbox, {'первая': 'есть'})  # у «второй» круг не кончится
    rows = dsh_board.dsh_board_fanout([{'cwd': '/p/1', 'text': 'первая'},
                                       {'cwd': '/p/2', 'text': 'вторая'}], timeout=5)
    assert rows[0]['answer'] == 'есть' and not rows[0]['error']
    assert 'DshBoardError' in rows[1]['error'] and rows[1]['session'] == 'card-2'
    drops = [e[3]['goal'] for e in events if e[0] == 'tool' and e[2] == 'continuous_drop']
    assert drops == ['goal-1']


def _box(monkeypatch, sandbox, answers):
    """Хост-эмулятор бокса: карточки, засечки, клады и снятия — в `events`, ответы — из `answers`.

    `answers` — {текст задачи: текст ответа}; задачи там, где её нет, ответа не дождутся.
    `events` — лента: ('session', body) завод карточек, ('cursor', карточка),
    ('tool', карточка, инструмент, аргументы), ('wait', карточка, текст, засечка).
    """
    events, goals, cards = [], [], {'n': 0}
    monkeypatch.delenv('CLAUDE_CODE_SESSION_ID', raising=False)

    def post(route, body, **_kw):
        if route == 'continuous-agent.session':
            cards['n'] += 1
            events.append(('session', body))
            return {'session': f"card-{cards['n']}"}
        if route == 'continuous-agent.tool':
            events.append(('tool', body['session'], body['name'], body['args']))
            if body['name'] == 'continuous_add':
                goals.append({'id': f"goal-{len(goals) + 1}", 'session': body['session'],
                              'text': body['args']['text']})
            return {'value': {'goals': list(goals)}}
        return {}

    def get(route, **_kw):
        return {'workspaces': [], 'robots': [], 'archived': [],
                'models': [MODEL], 'sessionModel': MODEL}

    def turn_reply(session, mark, asked, timeout, **_kw):
        events.append(('wait', session, asked, mark))
        answer = answers.get(asked)
        if answer is None:
            return None                                    # круг ещё идёт — не дождались
        return {'text': answer, 'reason': 'completed', 'seq': mark + 1}

    monkeypatch.setattr(dsh_board, 'dsh_api_post', post)
    monkeypatch.setattr(dsh_board, 'dsh_api_get', get)
    monkeypatch.setattr(dsh_session, 'dsh_api_call',
                        lambda route, body, **_kw: sandbox.calls.append((route, body)) or {})
    monkeypatch.setattr(dsh_board, 'dsh_session_cursor',
                        lambda session, **_kw: events.append(('cursor', session)) or 11)
    monkeypatch.setattr(dsh_board, 'dsh_session_turn_reply', turn_reply)
    return events


def _tree(path=COPY):
    return {'path': path, 'branch': 'dsh/pravka', 'root': '/repo'}


def _dsh_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


# ── срок круга: чем мерить --timeout ──

def test_eta_measures_running_round_and_suggests_ceiling(sandbox, monkeypatch):
    """`eta`: круг идёт 10 мин, готовые круги этой карточки — 20 и 13 мин → потолок 15 мин."""
    import time
    now = time.time()
    monkeypatch.setattr(dsh_board, 'dsh_session_turn_log', lambda session, rounds=0, **kw: [
        {'turn': 1, 'start': now - 2400, 'end': now - 1200},
        {'turn': 2, 'start': now - 1100, 'end': now - 300},
        {'turn': 3, 'start': now - 600, 'end': None}])

    eta = dsh_board.dsh_board_eta('card-1')

    assert eta['running'] and 590 <= eta['elapsed'] <= 610
    assert eta['rounds'] == [800, 1200] and eta['typical'] == 1000
    assert eta['suggest'] == 1500                      # медиана с запасом `DSH_BOARD_ETA_MARGIN`


def test_eta_without_finished_rounds_keeps_common_ceiling(sandbox, monkeypatch):
    """Карточка без готовых кругов: срок не выдумывается, остаётся общий потолок."""
    monkeypatch.setattr(dsh_board, 'dsh_session_turn_log', lambda session, rounds=0, **kw: [])

    assert dsh_board.dsh_board_eta('card-1') == {'running': False, 'elapsed': 0, 'rounds': [],
                                                 'typical': 0, 'suggest': dsh_board.DSH_BOARD_WAIT_TIMEOUT}


def test_eta_young_running_round_is_measured_by_common_ceiling(sandbox, monkeypatch):
    """Первый круг свежей карточки идёт 10 мин: предлагают общий потолок, а не `elapsed × запас`."""
    import time
    now = time.time()
    monkeypatch.setattr(dsh_board, 'dsh_session_turn_log', lambda session, rounds=0, **kw: [
        {'turn': 1, 'start': now - 600, 'end': None}])

    eta = dsh_board.dsh_board_eta('card-1')

    assert eta['running'] and eta['typical'] == 0
    assert eta['suggest'] == int(dsh_board.DSH_BOARD_WAIT_TIMEOUT * dsh_board.DSH_BOARD_ETA_MARGIN)


def test_eta_shows_latest_rounds_not_the_longest(sandbox, monkeypatch):
    """Показаны последние круги, а не самые долгие: длинный старый круг не вытесняет свежие."""
    import time
    now = time.time()
    monkeypatch.setattr(dsh_board, 'dsh_session_turn_log', lambda session, rounds=0, **kw: [
        {'turn': 1, 'start': now - 5000, 'end': now - 3800},     # 1200 с — самый долгий
        {'turn': 2, 'start': now - 3000, 'end': now - 2900},
        {'turn': 3, 'start': now - 2000, 'end': now - 900},
        {'turn': 4, 'start': now - 800, 'end': now - 710}])

    eta = dsh_board.dsh_board_eta('card-1')

    assert eta['rounds'] == [90, 100, 1100] and eta['typical'] == 100
    assert eta['suggest'] == 150                       # 100 × запас, без оглядки на круг в 20 мин


# ── пара «задача — ответ» для проверки ──

def _verify_setup(sandbox, monkeypatch, pair, asked):
    """Проверяемая карточка — обычная (не копия): проверяющий встаёт в её каталог."""
    monkeypatch.setattr(dsh_board, 'dsh_session_find', lambda session, **kw: {'cwd': '/some/project'})
    monkeypatch.setattr(dsh_board, 'dsh_session_round_pair', lambda session, **kw: pair)
    monkeypatch.setattr(dsh_board, 'dsh_session_title', lambda item: 'правка')
    monkeypatch.setattr(dsh_board, 'dsh_board_ask', lambda session, text, *a, **kw: asked.append(text) or 'вердикт')
    monkeypatch.setattr(dsh_board, 'dsh_board_worktree_row', lambda session: {})


def test_verify_takes_task_from_journal_not_from_tail(sandbox, monkeypatch):
    """Задача далеко в журнале — вердикт всё равно ставится: пара берётся по всему журналу."""
    asked = []
    _verify_setup(sandbox, monkeypatch, {'task': 'собери статистику', 'answer': 'готово', 'via': 'user'}, asked)
    dsh_board.dsh_board_verify('card-1', base='x:1')
    assert 'собери статистику' in asked[0] and 'готово' in asked[0]


def test_verify_without_task_in_journal_asks_for_it_by_words(sandbox, monkeypatch):
    """Ответ есть, задачи в журнале нет — ошибка говорит, что задачу можно назвать словами."""
    asked = []
    _verify_setup(sandbox, monkeypatch, {'task': '', 'answer': 'готово', 'via': ''}, asked)
    with pytest.raises(dsh_board.DshBoardError, match='--task'):
        dsh_board.dsh_board_verify('card-1', base='x:1')
    assert asked == []

    dsh_board.dsh_board_verify('card-1', task='сверь цифры отчёта', base='x:1')
    assert 'сверь цифры отчёта' in asked[0]


def test_working_line_gains_eta_only_when_asked():
    """Без `--eta` строка `working` не меняется; с ним — срок круга и потолок для `--timeout`."""
    row = {'motor': 2, 'session': 'card-1', 'title': 'ревью', 'goal': 'сверяй логи'}
    assert dsh_board_cli._working_text([row]) == 'мотор 2  ревью  — сверяй логи'
    line = dsh_board_cli._working_text([dict(row, running=True, elapsed=1200, typical=0, suggest=1800)])
    assert 'круг идёт 20 мин, готовых кругов ещё не было, --timeout 1800' in line


def test_working_line_shows_queue_after_busy_motors():
    from dsh_.dsh_board_cli import _working_line
    rows = [{'motor': i, 'session': f's{i}', 'goal': 'g', 'title': ''} for i in (1, 2, 3, 4)]
    assert _working_line({'working': rows, 'capacity': 4, 'queued': 2}).startswith('DSH 4/4 +2: ')
    assert _working_line({'working': rows, 'capacity': 4, 'queued': 0}).startswith('DSH 4/4: ')
    assert _working_line({'working': [], 'capacity': 4}) == 'DSH 0/4'


# ── отмена карточки: круг и очередь ──

def _cancel_host(sandbox, monkeypatch, *, next_step=(), next_turn=(), running=False, cold=False):
    """Хост с очередью карточки: `session/projections` отдаёт `inbox`, `updateQueue` снимает.

    `cold` — агент не прикреплён, `session/cancel` отвечает `session/not-found` (так бывает
    у карточки, чей промпт просто лежит в очереди); `running` — список хоста показывает круг.
    """
    state = {'next-turn': [dict(m) for m in next_turn], 'next-step': [dict(m) for m in next_step]}

    def call(route, body, **_kw):
        sandbox.calls.append((route, body))
        if route == 'session/projections':
            return {'asOfSeq': 14, 'values': {'inbox': {k: list(v) for k, v in state.items()}}}
        if route == 'session/updateQueue':
            gone = body['request']['itemId']
            for target in state.values():
                target[:] = [m for m in target if m.get('id') != gone]
            return {'accepted': True}
        return {}

    def cancel(session, **_kw):
        sandbox.calls.append(('session/cancel', {'sessionId': session}))
        if cold:
            raise dsh_board.DshApiError('session/not-found: карточка не прикреплена')
        return {}

    monkeypatch.setattr(dsh_board, 'dsh_api_call', call)
    monkeypatch.setattr(dsh_board, 'dsh_session_find',
                        lambda session, **kw: {'sessionId': session, 'running': running})
    monkeypatch.setattr(dsh_board, 'dsh_session_cancel', cancel)
    monkeypatch.setattr(dsh_board, 'dsh_board_box_drop', lambda session, **kw: [])
    monkeypatch.setattr(dsh_board, 'dsh_session_archive',
                        lambda session, **kw: sandbox.calls.append(('workspace/archiveSession',
                                                                    {'sessionId': session})) or [])
    return state


def test_cancel_queued_card_removes_parked_prompt(sandbox, monkeypatch):
    """`session/cancel` очередь бережёт (`keepInbox`) — снятый с глаз промпт всё равно пошёл бы.

    Отмена обязана снять его из `inbox` карточки (`session/updateQueue`, действие `remove`),
    иначе «отменённая» карточка дождётся свободного мотора и начнёт круг.
    """
    state = _cancel_host(sandbox, monkeypatch, next_step=[{'id': 'parked-1'}], cold=True)
    result = dsh_board.dsh_board_cancel('continuous-card')
    assert result == {'stopped': False, 'removed': ['parked-1'], 'dropped': []}
    removals = [c for c in sandbox.calls if c[0] == 'session/updateQueue']
    assert len(removals) == 1                          # второй заход очереди пуст — дублей нет
    assert removals[0][1]['request'] == {'sessionId': 'continuous-card', 'itemId': 'parked-1',
                                         'action': {'kind': 'remove'}}
    assert state['next-step'] == []


def test_cancel_running_card_interrupts_round(sandbox, monkeypatch):
    """Идущий круг прерван `session/cancel`, а ждущий после него промпт снят с очереди."""
    _cancel_host(sandbox, monkeypatch, next_turn=[{'id': 'wait-1'}], running=True)
    result = dsh_board.dsh_board_cancel('continuous-card')
    assert result == {'stopped': True, 'removed': ['wait-1'], 'dropped': []}
    assert ('session/cancel', {'sessionId': 'continuous-card'}) in sandbox.calls


def test_cancel_without_round_and_queue_is_empty(sandbox, monkeypatch):
    """Ни круга, ни очереди — отмена молчалива и безопасна: `{'stopped': False, 'removed': [], 'dropped': []}`."""
    _cancel_host(sandbox, monkeypatch, cold=True)
    assert dsh_board.dsh_board_cancel('continuous-card') == {'stopped': False, 'removed': [], 'dropped': []}


def test_cancel_unknown_card_raises(sandbox, monkeypatch):
    """Опечатка в id — ошибка хоста, а не «нечего отменять»: молча съеденная опечатка дороже."""
    def call(route, body, **_kw):
        sandbox.calls.append((route, body))
        return None                                    # проекции нет — карточки на хосте нет

    def cancel(session, **_kw):
        raise dsh_board.DshApiError('session/not-found: карточки нет')

    monkeypatch.setattr(dsh_board, 'dsh_api_call', call)
    monkeypatch.setattr(dsh_board, 'dsh_session_find', lambda session, **kw: {})
    monkeypatch.setattr(dsh_board, 'dsh_session_cancel', cancel)
    with pytest.raises(dsh_board.DshApiError, match='not-found'):
        dsh_board.dsh_board_cancel('continuous-нет-такой')


def test_archive_cancels_queued_before_archiving(sandbox, monkeypatch):
    """Архив промпт из очереди не снимает — archive сначала отменяет, потом убирает в архив."""
    _cancel_host(sandbox, monkeypatch, next_step=[{'id': 'parked-1'}], cold=True)
    sandbox.home.mkdir(exist_ok=True)                 # DSH_HOME в tmp создаётся лениво, как в бою
    dsh_wait_mark = sandbox.home / 'dsh-board.expected.json'
    dsh_wait_mark.write_text('{"continuous-card": {"owner": "orch-1", "at": 1}}')

    result = dsh_board.dsh_board_archive('continuous-card')

    assert result['removed'] == ['parked-1']
    routes = [c[0] for c in sandbox.calls]
    assert routes.index('session/updateQueue') < routes.index('workspace/archiveSession')
    assert json.loads(dsh_wait_mark.read_text()) == {}    # метка снята — в архиве ждать нечего


def test_cli_cancel_reports_what_it_stopped(sandbox, monkeypatch, capsys):
    """CLI `cancel` словами говорит, что сделал: круг, очередь — или что отменять было нечего."""
    monkeypatch.setattr(dsh_board_cli, 'dsh_board_cancel',
                        lambda session, **kw: {'stopped': True, 'removed': ['a', 'b'], 'dropped': []})
    assert dsh_board_cli_main(['cancel', 'continuous-card']) == 0
    assert capsys.readouterr().out.strip() == 'прерван идущий круг; снято из очереди: 2'

    monkeypatch.setattr(dsh_board_cli, 'dsh_board_cancel',
                        lambda session, **kw: {'stopped': False, 'removed': [], 'dropped': []})
    assert dsh_board_cli_main(['cancel', 'continuous-card']) == 0
    assert capsys.readouterr().out.strip() == 'нечего отменять'

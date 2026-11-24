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
    assert asked and asked[0].startswith('Твоя копия репозитория: ' + COPY)
    assert 'cd ' + COPY in asked[0] and '/repo не трогай' in asked[0]
    assert json.loads(sandbox.meta.read_text())['card-1']['path'] == COPY


# ── поиск копии карточкой ──

def test_worktree_row_reads_metadata_by_generation_key(sandbox):
    """Метаданные — по ключу карточки: поколение `-gN` не ломает поиск."""
    dsh_board.dsh_board_session_new(COPY, 'правка', worktree=_tree())
    assert dsh_board.dsh_board_worktree_row('card-1-g3')['path'] == COPY
    assert dsh_board.dsh_board_worktree_row('чужая') == {}


def test_cli_land_finds_copy_by_metadata(sandbox, monkeypatch, capsys):
    """`land` новой карточки берёт копию из метаданных, а не из cwd (там техworkspace)."""
    landed = []
    monkeypatch.setattr(dsh_board_cli, 'dsh_session_find',
                        lambda session, **kw: {'cwd': str(sandbox.technical)})
    monkeypatch.setattr(dsh_board_cli, 'dsh_session_archive', lambda session, **kw: None)
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
    monkeypatch.setattr(dsh_board_cli, 'dsh_session_archive', lambda session, **kw: None)
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
    # техworkspace на панели и каталога его нет вовсе — всё равно не снимается.
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


def test_workspace_drop_refuses_technical(sandbox):
    """Снять технический workspace руками — отказ, его файлы и карточки общие."""
    sandbox.state['workspaces'] = [{'id': 'w-tech', 'path': str(sandbox.technical)}]
    assert dsh_board.dsh_board_workspace_drop(str(sandbox.technical), base='x:1') is False
    assert sandbox.calls == []


# ── промпт проверяющего ──

def test_verify_of_worktree_card_gets_head(sandbox, monkeypatch):
    """Карточка-проверяющий копии — тоже в техworkspace, с шапкой о пути копии."""
    monkeypatch.setattr(dsh_board, 'dsh_session_find',
                        lambda session, **kw: {'cwd': str(sandbox.technical), 'projections': {}})
    asked = []

    def rows(session, count, **kw):
        return [{'role': 'user', 'text': 'задача'}, {'role': 'assistant', 'text': 'ответ'}]

    monkeypatch.setattr(dsh_board, 'dsh_session_dialog', rows)
    monkeypatch.setattr(dsh_board, 'dsh_session_title', lambda item: 'правка')
    monkeypatch.setattr(dsh_board, 'dsh_board_ask',
                        lambda session, text, *a, **kw: asked.append(text) or 'вердикт')
    _dsh_json(sandbox.meta, {'card-1': {'path': COPY, 'branch': 'dsh/pravka', 'root': '/repo'}})
    dsh_board.dsh_board_verify('card-1', base='x:1')
    assert asked[0].startswith('Твоя копия репозитория: ' + COPY)
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

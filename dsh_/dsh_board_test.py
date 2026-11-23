"""Тесты одной технической worktree-workspace в `dsh_board`.

Карточка `--worktree` не плодит на панели новый проект: хост не принимает cwd отдельно
от workspace (см. DSH_BOARD_WORKTREE_WORKSPACE), и копия доносится карточке метаданными
и шапкой промпта. Сеть не трогается: `dsh_api_post`/`dsh_api_get`/`dsh_api_call` и git
подменены; `DSH_HOME` — в `tmp_path`, чтобы не писать в `~/.dsh`.
"""
import json
import os

import pytest

from dsh_ import dsh_board, dsh_board_cli
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


def _tree(path=COPY):
    return {'path': path, 'branch': 'dsh/pravka', 'root': '/repo'}


def _dsh_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')

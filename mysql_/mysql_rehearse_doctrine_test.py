"""`mysql_rehearse_doctrine_test`: docker, mysql-клиент и php-прогон — подставками; git не нужен."""
import re
import subprocess
import tempfile
import sys
from pathlib import Path

# pytest подсовывает в sys.path каталог этого файла, и `import mysql_` спотыкается
# о соседний `mysql_.py`; разворачиваем пути так же, как делают сами модули слоя.
_here = str(Path(__file__).resolve().parent)
sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import mysql_.mysql_rehearse_doctrine as doc

NEW_MIGRATION = """<?php
final class Version20240202000002 extends AbstractMigration
{
    public function up(Schema $schema): void
    {
        $this->addSql('INSERT INTO payment_provider_info (a, b) VALUES (1, 2)');
        $this->addSql('UPDATE bar SET x = 1');
        $this->addSql("INSERT INTO bar (a) VALUES (1) ON DUPLICATE KEY UPDATE a = a");
        $this->addSql('CREATE TABLE ghost (id INT NOT NULL)');
        $this->addSql('ALTER TABLE bar ADD col DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('DELETE FROM payment_provider_info WHERE a = 1');
        $this->addSql('DROP TABLE ghost');
    }
}
"""

OLD_MIGRATION = """<?php
final class Version20240101000001 extends AbstractMigration
{
    public function up(Schema $schema): void
    {
        $this->addSql('CREATE TABLE old_table (id INT NOT NULL)');
        $this->addSql('CREATE UNIQUE INDEX u ON old_table (id)');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('TRUNCATE TABLE old_table');
        $this->addSql('DROP TABLE old_table');
    }
}
"""


def _repo(tmp_path):
    """Symfony-подобная выкладка: миграции в `src/Migrations` по пути из doctrine_migrations.yaml."""
    root = tmp_path / 'app'
    cfg = root / 'config' / 'packages' / 'doctrine_migrations.yaml'
    cfg.parent.mkdir(parents=True)
    cfg.write_text("doctrine_migrations:\n    migrations_paths:\n"
                   "        'DoctrineMigrations': '%kernel.project_dir%/src/Migrations'\n", encoding='utf-8')
    mig = root / 'src' / 'Migrations'
    mig.mkdir(parents=True)
    (mig / 'Version20240101000001.php').write_text(OLD_MIGRATION, encoding='utf-8')
    (mig / 'Version20240202000002.php').write_text(NEW_MIGRATION, encoding='utf-8')
    (root / 'bin').mkdir()
    (root / 'bin' / 'console').write_text('#!/bin/sh\n', encoding='utf-8')
    return root


def _fakes(monkeypatch, state, tmp_path):
    """Подменяет php-прогон и весь `subprocess`; возвращает (docker-вызовы, php-вызовы, журнал-состояние).

    Подменный `subprocess` не трогает диск, а директорию секретов модуль делает через
    `tempfile` — переселяем `tempfile` в `tmp_path`, чтобы pytest убрал за собой.

    `state`: `tables` — живые таблицы базы, `ready_after` — сколько polls до готовности,
    `skew` — второй up даст иные числа, `fail` — фаза падает, `check_fail` — sql-check отказывает.
    """
    docker_calls, php_calls = [], []
    real = subprocess.run
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(doc.tempfile, 'mkdtemp',
                        lambda prefix='tmp', **kw: real_mkdtemp(prefix=prefix, dir=str(tmp_path)))
    monkeypatch.setattr(doc.tempfile, 'gettempdir', lambda: str(tmp_path))

    def php_run(repo='', worktree='', argv=None, env=None, networks=None, timeout=None):
        php_calls.append({'argv': [str(one) for one in (argv or [])], 'env': dict(env or {}),
                          'networks': list(networks or [])})
        cmd = ' '.join(php_calls[-1]['argv'])
        if state.get('fail'):
            state['fail'] = False
            return {'ok': False, 'code': 1, 'out': 'SQLSTATE[42S02] Base table not found', 'log': ''}
        if ' prev' in cmd:
            state['tables'].pop('ghost', None)
            state['tables']['payment_provider_info'], state['tables']['bar'] = 0, 5
        elif 'migrate' in cmd:
            state['up_runs'] = state.get('up_runs', 0) + 1
            state['tables'].update(payment_provider_info=2, bar=6, ghost=0)
            if state['up_runs'] == 2 and state.get('skew'):
                state['tables']['payment_provider_info'] = 4
        return {'ok': True, 'code': 0, 'out': 'Done', 'log': ''}

    def run(cmd, *args, **kwargs):
        if isinstance(cmd, list):
            docker_calls.append([str(one) for one in cmd])
            return subprocess.CompletedProcess(cmd, 0, '', '')
        text = str(cmd)
        if not text.startswith('docker exec'):
            return real(cmd, *args, **kwargs)
        sql = (kwargs.get('input') or '').strip()
        if 'SHOW DATABASES' in sql.upper():
            state['polls'] = state.get('polls', 0) + 1
            if state['polls'] > state.get('ready_after', 0):
                return subprocess.CompletedProcess(cmd, 0, 'rehearse\n', '')
            return subprocess.CompletedProcess(cmd, 1, '', 'query was empty')
        if sql.upper().startswith('SELECT COUNT(*) FROM'):
            if ' WHERE ' in sql.upper():                        # контрольный запрос --sql-check
                if state.get('check_fail'):
                    return subprocess.CompletedProcess(cmd, 1, '', 'ERROR 1064 syntax')
                return subprocess.CompletedProcess(cmd, 0,
                                                   f"{state['tables'].get('payment_provider_info', 0)}\n", '')
            found = re.search(r'FROM\s+`?([A-Za-z0-9_]+)`?', sql, re.I)
            name = found.group(1) if found else '?'
            if name in state['tables']:
                return subprocess.CompletedProcess(cmd, 0, f"{state['tables'][name]}\n", '')
            return subprocess.CompletedProcess(cmd, 1, '', f'ERROR 1146: no such table {name}')
        if state.get('check_fail'):
            return subprocess.CompletedProcess(cmd, 1, '', 'ERROR 1064 syntax')
        return subprocess.CompletedProcess(cmd, 0, f"{state['tables'].get('payment_provider_info', 0)}\n", '')

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(doc, 'project_worktree_php_run', php_run)
    monkeypatch.setattr(doc.time, 'sleep', lambda one: None)
    return docker_calls, php_calls


def test_tables_parsed_from_add_sql(tmp_path):
    root = _repo(tmp_path)
    tables = doc.mysql_rehearse_doctrine_tables([root / 'src' / 'Migrations' / 'Version20240202000002.php'])
    assert tables == ['bar', 'ghost', 'payment_provider_info']   # без CURRENT_TIMESTAMP и без columns из ON DUPLICATE


def test_tables_parse_edge_statements(tmp_path):
    way = tmp_path / 'V.php'
    way.write_text("""$this->addSql('-- комментарий: ALTER TABLE fake_in_comment');
        $this->addSql('RENAME TABLE a TO b');
        $this->addSql('DROP INDEX i ON keep_me');
        $this->addSql('REPLACE INTO replaced (a) VALUES (1)');""", encoding='utf-8')
    assert doc.mysql_rehearse_doctrine_tables([way]) == ['a', 'b', 'keep_me', 'replaced']


def test_full_cycle_counts_and_cleanup(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    state = {'tables': {'payment_provider_info': 0, 'bar': 5}}
    docker_calls, php_calls = _fakes(monkeypatch, state, tmp_path)
    result = doc.mysql_rehearse_doctrine_run(str(root), sql_check="SELECT COUNT(*) FROM payment_provider_info WHERE payment_method LIKE 'RAPYD_%'")

    assert [one['label'] for one in result['phases']] == ['up', 'prev', 'up again']
    assert result['ok'] and result['idempotent']
    up, down, up_again = result['phases']
    assert up['counts'] == {'payment_provider_info': 2, 'bar': 6, 'ghost': 0}
    assert down['counts']['payment_provider_info'] == 0 and down['counts']['ghost'] is None
    assert down['delta']['payment_provider_info'] == -2 and up_again['delta']['payment_provider_info'] == 2
    assert [one['check'] for one in result['phases']] == ['2', '0', '2']
    assert 'идемпотентно up→down→up: да' in result['digest']

    # база — своя, приватная сеть — своя; php-прогон видит только её
    net = next(one[3] for one in docker_calls if one[:2] == ['docker', 'network'] and one[2] == 'create')
    assert all(one['networks'] == [net] for one in php_calls)
    url = php_calls[0]['env']['DATABASE_URL']
    assert url.startswith('mysql://root:') and url.endswith(':3306/rehearse')
    ctr = url.split('@')[1].split(':')[0]
    assert any(one[:4] == ['docker', 'rm', '-f', '-v'] and one[4] == ctr for one in docker_calls)
    assert any(one[:3] == ['docker', 'network', 'rm'] and one[3] == net for one in docker_calls)
    # пароль — только в смонтированном файле: ни в argv, ни в журнале
    password = url.split('root:')[1].split('@')[0]
    assert password not in ' '.join(' '.join(one) for one in docker_calls)
    assert password not in open(result['log'], encoding='utf-8').read()


def test_prev_twice_rolls_two_migrations(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    state = {'tables': {'payment_provider_info': 0, 'bar': 5, 'old_table': 3}}
    _, php_calls = _fakes(monkeypatch, state, tmp_path)
    result = doc.mysql_rehearse_doctrine_run(str(root), prev=2)
    assert len(result['migrations']) == 2
    assert 'old_table' in result['tables']
    assert len(php_calls) == 4                                    # up, prev, prev, up again
    assert result['phases'][1]['codes'] == [0, 0]
    assert ' prev' in ' '.join(php_calls[2]['argv'])


def test_skew_breaks_idempotence(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    state = {'tables': {'payment_provider_info': 0, 'bar': 5}, 'skew': True}
    _fakes(monkeypatch, state, tmp_path)
    result = doc.mysql_rehearse_doctrine_run(str(root))
    assert result['ok'] and not result['idempotent']
    assert '⚠ нет' in result['digest']


def test_failed_phase_still_cleans(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    state = {'tables': {'payment_provider_info': 0, 'bar': 5}, 'fail': True}
    docker_calls, _ = _fakes(monkeypatch, state, tmp_path)
    result = doc.mysql_rehearse_doctrine_run(str(root))
    assert not result['ok'] and not result['idempotent']
    assert any(one[:2] == ['docker', 'rm'] for one in docker_calls)
    assert any(one[:3] == ['docker', 'network', 'rm'] for one in docker_calls)


def test_ready_wait_polls_until_database_exists(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    state = {'tables': {'payment_provider_info': 0, 'bar': 5}, 'ready_after': 3}
    docker_calls, _ = _fakes(monkeypatch, state, tmp_path)
    result = doc.mysql_rehearse_doctrine_run(str(root))
    assert result['ok'] and state['polls'] >= 4                  # не поверил временному серверу без базы
    assert any(one[:2] == ['docker', 'rm'] for one in docker_calls)


def test_sql_check_error_is_reported_not_raised(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    state = {'tables': {'payment_provider_info': 0, 'bar': 5}, 'check_fail': True}
    _fakes(monkeypatch, state, tmp_path)
    result = doc.mysql_rehearse_doctrine_run(str(root), sql_check='BROKEN SQL')
    assert all(str(one['check']).startswith('ошибка') for one in result['phases'])
    assert result['idempotent']                                  # равные отказы — тоже равенство


def test_missing_migrations_dir_raises(tmp_path, monkeypatch):
    _fakes(monkeypatch, {'tables': {}}, tmp_path)
    with pytest.raises(RuntimeError):
        doc.mysql_rehearse_doctrine_run(str(tmp_path / 'nowhere'))

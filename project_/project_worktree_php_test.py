"""`project_worktree_php_test`: docker смонтирован подставкой, git-копия настоящая."""
import json
import os
import subprocess

import project_.project_worktree_php as php


def _git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _commit(cwd, *paths):
    _git(cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '-A', *paths)
    _git(cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'правка')


def _write(way, text):
    way.parent.mkdir(parents=True, exist_ok=True)
    way.write_text(text, encoding='utf-8')


def _repo(tmp_path):
    """Основная выкладка: master с a.php/c.php и gitignored `vendor/`, `.env`, `bin/phpunit`."""
    repo = tmp_path / 'app'
    _write(repo / '.gitignore', "/vendor\n/.env\n/behat.yml\n/bin/.phpunit\n")
    _write(repo / 'a.php', "<?php\n// base\n")
    _write(repo / 'c.php', "<?php\n// base\n")
    _write(repo / 'bin' / 'phpunit', "#!/bin/sh\necho phpunit\n")
    (repo / 'bin' / 'phpunit').chmod(0o755)
    _write(repo / 'ruleset.xml', '<ruleset/>\n')
    for one in ('behat', 'phpcs', 'psalm'):
        _write(repo / 'vendor' / 'bin' / one, "#!/bin/sh\necho %s\n" % one)
    _write(repo / 'vendor' / 'autoload.php', "<?php\n")
    _write(repo / '.env', "SECRET=never-in-argv\n")
    _write(repo / 'behat.yml', "default:\n    suites: ~\n")
    _git(repo, 'init', '-q', '-b', 'master')
    _git(repo, 'remote', 'add', 'origin', str(tmp_path / 'origin.git'))   # link пустеет, базы — local
    _commit(repo)
    return repo


def _worktree(repo, tmp_path, name='wt', branch='feature'):
    tree = tmp_path / name
    _git(repo, 'branch', branch)
    _git(repo, 'worktree', 'add', '-q', '--detach', str(tree), branch)
    return tree


def _config(repo):
    return {
        'name': 'Shop',
        'services': {
            'phpmyadmin': {'image': 'phpmyadmin:latest'},
            'mysql': {'image': 'mysql:8', 'networks': {'shop_net': None}},
            'php-fpm': {'build': {'context': '.'},
                        'volumes': [{'type': 'bind', 'source': str(repo), 'target': '/usr/src/app'}],
                        'networks': {'shop_net': None},
                        'environment': {'DATABASE_HOST': 'mysql', 'DATABASE_PASSWORD': 'secret'}},
        },
        'networks': {'shop_net': {'name': 'shop_net', 'external': True}},
    }


def _fake_docker(monkeypatch, config, outputs=()):
    """Вся `docker` — подставка (вызовы в `calls`, тексты env-файлов в `envs`); `git` — настоящий."""
    calls, envs, real = [], [], subprocess.run

    def run(argv, *args, **kwargs):
        argv = [str(one) for one in argv]
        if argv[0] != 'docker':
            return real(argv, *args, **kwargs)
        calls.append(argv)
        if '--env-file' in argv:
            envs.append(open(argv[argv.index('--env-file') + 1], encoding='utf-8').read())
        if argv[1:2] == ['compose']:
            return subprocess.CompletedProcess(argv, 0, json.dumps(config), '')
        if ':/sweep' in ' '.join(argv):                    # уборка артефактов
            return subprocess.CompletedProcess(argv, 0, '', '')
        text, rc = outputs.pop(0) if outputs else ('', 0)
        return subprocess.CompletedProcess(argv, rc, text, '')

    monkeypatch.setattr(php.subprocess, 'run', run)
    return calls, envs


def test_service_pick_prefers_code_mount(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    calls, _ = _fake_docker(monkeypatch, _config(repo), [('OK (3 tests, 5 assertions)\n', 0)])
    result = php.project_worktree_php_test(str(repo), str(tree), 'phpunit')
    run = next(a for a in calls if a[:2] == ['docker', 'run'] and ':/sweep' not in ' '.join(a))
    assert 'shop-php-fpm' in run                       # build-only образ: имя проекта + сервис
    assert run[run.index('--network') + 1] == 'shop_net'
    assert result['ok'] and result['passed'] == 3


def test_mounts_filter_and_env_not_in_argv(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    calls, envs = _fake_docker(monkeypatch, _config(repo), [('OK (1 tests, 1 assertions)\n', 0)])
    php.project_worktree_php_test(str(repo), str(tree), 'phpunit', filt='Rapyd')
    run = next(a for a in calls if a[:2] == ['docker', 'run'] and ':/sweep' not in ' '.join(a))
    assert f'{tree}:/usr/src/app' in run                             # код — из копии
    assert f'{repo}/vendor:/usr/src/app/vendor:ro' in run            # gitignored-каталог — из основной ro
    assert any(one.endswith(':/php-stage:ro') for one in run)        # gitignored-файлы — staging'ом
    assert any(one.startswith('cp -f /php-stage/.env') for one in run)
    assert f'{repo}/.env' not in ' '.join(run)                      # bind-файлом внутрь копии — нельзя
    assert '--env-file' in run and 'DATABASE_PASSWORD=secret' in envs[0]
    assert not any('secret' in part for part in run)                 # секреты не в командной строке
    assert run[run.index('--filter') + 1] == 'Rapyd'
    assert 'bin/phpunit' in run                                      # bin из основной, если в копии нет


def test_phpunit_digest_failures_and_sweep(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    out = ('Tests: 7, Assertions: 20, Errors: 0, Failures: 1\n'
           "1) App\\Test\\RapydTest::test_wallet\nsome trace\n")
    calls, _ = _fake_docker(monkeypatch, _config(repo), [(out, 1)])
    result = php.project_worktree_php_test(str(repo), str(tree), 'phpunit', filt='Rapyd')
    assert (result['code'], result['ok'], result['failed'], result['passed']) == (1, False, 1, 6)
    assert result['failures'] == ["App\\Test\\RapydTest::test_wallet"]
    assert 'Tests: 7' in result['digest'] and 'Rapyd' in result['digest']
    sweep = next(a for a in calls if a[:2] == ['docker', 'run'] and ':/sweep' in ' '.join(a))
    assert '-v' in sweep and f'{tree}:/sweep' in sweep
    shell = sweep[sweep.index('sh') + 2]
    assert '/sweep/var' in shell and '.phpcs-cache' in shell and f'-user {os.getuid()}' in shell
    assert ['docker', 'rm', '-f'] == [x for x in calls if x[:3] == ['docker', 'rm', '-f']][0][:3]


def test_phpcs_delta_against_base(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    _write(tree / 'a.php', "<?php\n// changed\n")
    _write(tree / 'b.php', "<?php\n// new\n")
    _git(tree, 'mv', 'c.php', 'd.php')
    _commit(tree)
    header = '"FILE","LINE","COLUMN","TYPE","MESSAGE","SOURCE"\n'
    now = header + '"/usr/src/app/a.php","10","1","ERROR","nope","S.E"\n' \
                   '"/usr/src/app/b.php","3","1","WARNING","tab","S.W"\n'
    base = header + '"/php-base/a.php","10","1","ERROR","nope","S.E"\n'
    calls, _ = _fake_docker(monkeypatch, _config(repo), [(now, 1), (base, 1)])
    result = php.project_worktree_php_test(str(repo), str(tree), 'phpcs')
    run = next(a for a in calls if a[:2] == ['docker', 'run'] and ':/sweep' not in ' '.join(a) and '/php-base' not in ' '.join(a))
    assert {'a.php', 'b.php', 'd.php'} <= set(run[-3:])              # гоним по изменённым
    assert '--standard=ruleset.xml' in run
    back = [a for a in calls if a[:2] == ['docker', 'run'] and '/php-base/c.php' in ' '.join(a)]
    assert back and '/php-base/a.php' in back[0]                     # переименованный — по старому пути в базе
    assert result['failed'] == 1                                     # ошибок, не предупреждений
    assert 'находок 2' in result['digest'] and 'дельта против базы +1 (было 1)' in result['digest']


def test_phpcs_skips_when_nothing_changed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    calls, _ = _fake_docker(monkeypatch, _config(repo))
    result = php.project_worktree_php_test(str(repo), str(tree), 'phpcs')
    assert result['ok'] and result['code'] == 0 and 'гнать нечего' in result['digest']
    assert [a for a in calls if a[:2] == ['docker', 'run'] and ':/sweep' not in ' '.join(a)] == []   # прогона не было


def test_behat_name_path_and_digest(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    out = ('--- Failed scenarios:\n\n    behat/features/rapyd/w.feature:12\n\n'
           '2 scenarios (1 passed, 1 failed)\n')
    calls, _ = _fake_docker(monkeypatch, _config(repo), [(out, 1)])
    result = php.project_worktree_php_test(str(repo), str(tree), 'behat',
                                           filt='Wallet', path='behat/features/rapyd')
    run = next(a for a in calls if a[:2] == ['docker', 'run'] and ':/sweep' not in ' '.join(a))
    assert run[-1] == 'behat/features/rapyd'
    assert run[run.index('--name', run.index('--name') + 1) + 1] == 'Wallet'   # второй — behat'овский
    assert result['failed'] == 1 and result['failures'] == ['behat/features/rapyd/w.feature:12']
    assert 'behat: 2 сценариев (1 passed, 1 failed)' in result['digest']


def test_empty_config_file_is_restaged(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    _write(tree / 'behat.yml', '')                                 # «точка монтирования» прошлого прогона
    calls, _ = _fake_docker(monkeypatch, _config(repo), [('2 scenarios (2 passed)\n', 0)])
    php.project_worktree_php_test(str(repo), str(tree), 'behat')
    run = next(a for a in calls if a[:2] == ['docker', 'run'] and ':/sweep' not in ' '.join(a))
    assert any('cp -f /php-stage/behat.yml' in one for one in run)   # пустой файл — не «файл есть»


def test_without_php_service_is_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    tree = _worktree(repo, tmp_path)
    config = _config(repo)
    config['services'] = {'nginx': {'image': 'nginx'}}
    _fake_docker(monkeypatch, config)
    try:
        php.project_worktree_php_test(str(repo), str(tree), 'phpunit')
        assert False, 'молча не проходит'
    except ValueError as err:
        assert 'nginx' in str(err)

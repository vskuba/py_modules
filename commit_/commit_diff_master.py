"""
Сверка ветки с мастером перед выкладкой: что поедет — коммиты, файлы, риски.

`git push` в мастер — это не «отправил тексты», это **запуск выкладки**: CI по
триггеру несёт HEAD на сервер, `bluegreen.sh` переключает слот. Смотреть при
этом в `git log --oneline` — видеть только первую половину правки: коммиты
рассказывают намерение, а на сервере решают файлы. Между коммитом и продом
рождаются три вещи, которые меняют не только код:

    миграции   — применятся к живой базе в момент выкладки;
    сабмодули  — уедет указатель, и чужой репозиторий обязан быть на origin
                 раньше, иначе `submodule update` на сервере не найдёт коммит;
    сборка     — docker-compose, Dockerfile, requirements: пересоберется образ.

Модуль отвечает одним взглядом: коммиты `base..HEAD`, diffstat от merge-base
(три точки: общая история не считается чужой правкой) и список флагов из этих
трёх. Та же пара — база и ветка — что у `commit_pr_view`, и тот же разбор
аргументов: ветка по умолчанию текущая, база — первая найденная.

    python -m commit_.commit_diff_master                # здесь против мастера
    python -m commit_.commit_diff_master feature/task_42 --diff   # и патч
"""
import argparse
import re
import subprocess

# Базы в порядке доверия — те же, что у `commit_pr_view`: своя ветка обязана
# сравниваться с общей, а не с тем, что первый попадётся.
_COMMIT_DIFF_MASTER_BASES = ('origin/master', 'master', 'origin/main', 'main')

# Пути, чья правка меняет не только текст программы: флаг получает метку и
# причину — чего ждать на выкладке.
_COMMIT_DIFF_MASTER_FLAGS = {
    'миграции': (r'migrations/', 'применятся к живой базе в момент выкладки'),
    'сборка': (r'(docker-compose.*\.yml|Dockerfile|requirements.*\.txt|docker/)',
               'образ пересоберется'),
    'CI': (r'\.github/workflows/', 'сама выкладка меняется — проверь порядок шагов'),
}


def commit_diff_master(branch: str = '', base: str = '', root: str = '.') -> dict:
    """Что поедет на выкладку: коммиты, файлы, риски.

    Args:
        branch: ветка; пусто — текущая.
        base: база; пусто — первая найденная из `origin/master`, `master`,
              `origin/main`, `main`.
        root: каталог репозитория.

    Returns:
        {'branch', 'base', 'merge_base', 'ahead', 'behind',
         'commits': [{'sha', 'subject'}],
         'files': [{'path', 'added', 'removed', 'binary', 'submodule'}],
         'totals': {'files', 'added', 'removed'},
         'flags': [{'kind', 'why', 'paths'}]}

    ⚠ `behind` — это то, что в базе уехало вперёд: перед push ветку стоит
    обновить, иначе diff показывает не то, что реально победит при слиянии.
    """
    branch = (branch or _git('rev-parse', '--abbrev-ref', 'HEAD', root)).strip()
    base = (base or next((one for one in _COMMIT_DIFF_MASTER_BASES
                          if _exists(one, root)), '')).strip()
    if not base:
        raise RuntimeError('не найдена база для сравнения — назови её прямо')

    head = _git('rev-parse', branch, root).strip()
    merge_base = _git('merge-base', base, head, root).strip()

    counts = _git('rev-list', '--left-right', '--count',
                  f'{base}...{head}', root).split()
    commits = [{'sha': sha, 'subject': subject} for sha, _, subject in
               (line.partition('\x1f') for line in
                _git('log', '--format=%h\x1f%s', f'{merge_base}..{head}',
                     root).splitlines())]

    # Три точки: сравнение от merge-base — общая история в правку не входит.
    names = _git('diff', '--raw', '--no-renames', f'{merge_base}...{head}',
                 root).splitlines()
    numbers = {}
    for line in _git('diff', '--numstat', '--no-renames',
                     f'{merge_base}...{head}', root).splitlines():
        added, _, rest = line.partition('\t')
        removed, _, path = rest.partition('\t')
        numbers[path] = (added, removed)

    files = []
    for line in names:
        # Формат raw — `:режим режим sha sha<TAB>статус<TAB>путь`: табы и
        # делят строку, путь последний, статус предпоследний.
        parts = line.split('\t')
        head, path = parts[0], parts[-1]
        added, removed = numbers.get(path, ('0', '0'))
        files.append({'path': path,
                      'binary': added == '-' or removed == '-',
                      'submodule': '160000' in head,
                      'added': int(added) if added != '-' else 0,
                      'removed': int(removed) if removed != '-' else 0})

    flags = []
    for kind, (pattern, why) in _COMMIT_DIFF_MASTER_FLAGS.items():
        hit = [f['path'] for f in files if _matches(pattern, f['path'])]
        if hit:
            flags.append({'kind': kind, 'why': why, 'paths': hit})
    sub = [f['path'] for f in files if f['submodule']]
    if sub:
        flags.append({'kind': 'сабмодули', 'why': 'указатель обязан быть '
                      'запушен в свой репозиторий ДО этой выкладки',
                      'paths': sub})

    return {'branch': branch, 'base': base, 'merge_base': merge_base[:7],
            'ahead': int(counts[1]), 'behind': int(counts[0]),
            'commits': commits, 'files': files,
            'totals': {'files': len(files),
                       'added': sum(f['added'] for f in files),
                       'removed': sum(f['removed'] for f in files)},
            'flags': flags}


def commit_diff_master_report(view: dict, patch: bool = False,
                              root: str = '.') -> str:
    """Исход словами: коммиты, файлы, итоги, флаги; с `patch` — и весь дифф."""
    lines = [f"`{view['branch']}` против `{view['base']}` "
             f"(база {view['merge_base']}): впереди {view['ahead']}, "
             f"позади {view['behind']}"]
    if view['behind']:
        lines.append('⚠ база ушла вперёд — обнови ветку, иначе сверяешь не то')

    for one in view['commits']:
        lines.append(f"  {one['sha']}  {one['subject'][:72]}")

    lines.append(f"  всего: {view['totals']['files']} файлов, "
                 f"+{view['totals']['added']} −{view['totals']['removed']}")
    for f in view['files']:
        if f['binary']:
            lines.append(f"    {f['path']}  бинарный")
        elif f['submodule']:
            lines.append(f"    {f['path']} (сабмодуль)")
        else:
            lines.append(f"    {f['path']}  +{f['added']} −{f['removed']}")

    for flag in view['flags']:
        lines.append(f"⚠ {flag['kind']}: {flag['why']}")
        for path in flag['paths']:
            lines.append(f"      · {path}")

    if patch:
        lines.append(_git('diff', f"{view['merge_base']}...{view['branch']}", root))
    return '\n'.join(lines)


def _exists(ref: str, root: str) -> bool:
    """Есть ли такая ссылка в этом репозитории вовсе."""
    return _run(['git', '-C', root, 'rev-parse', '--verify', '--quiet', ref],
                code=True) == 0


def _matches(pattern: str, path: str) -> bool:
    """Флаг по пути: регулярка без флагов, сбой regex — не совпадение."""
    try:
        return re.search(pattern, path) is not None
    except re.error:
        return False


def _git(*args) -> str:
    """git, чей вывод нужен; последним аргументом — каталог репо."""
    *git_args, root = args
    return _run(['git', '-C', root, *git_args])


def _run(argv, code: bool = False):
    """Команда; `code` — только код возврата, иначе stdout."""
    got = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if code:
        return got.returncode
    if got.returncode:
        raise RuntimeError(f'{" ".join(argv[:3])}…: {got.stderr.strip()[:200]}')
    return got.stdout


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Сверка ветки с мастером перед выкладкой: коммиты, файлы, риски.')
    ap.add_argument('branch', nargs='?', default='', help='ветка; пусто — текущая')
    ap.add_argument('--base', default='', help='база; пусто — первая найденная')
    ap.add_argument('--root', default='.', help='каталог репозитория')
    ap.add_argument('--diff', action='store_true', help='дописать полный патч')
    args = ap.parse_args()

    try:
        view = commit_diff_master(args.branch, args.base, args.root)
        print(commit_diff_master_report(view, args.diff, args.root))
        raise SystemExit(1 if view['flags'] or view['behind'] else 0)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

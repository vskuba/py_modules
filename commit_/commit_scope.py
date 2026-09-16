"""
Стаджинг своих путей в дереве, где работают несколько: свои файлы, чужие строки.

В рабочем дереве правит параллельная смена — чужие изменения соседствуют со
своими в одном `git status`. Брать всё звёздочкой — утащить чужое в свой коммит;
брать по памяти — утащить половинкой. Плюс правило проекта: правка модуля едет
одним изменением со строкой дока — удобно забыть докстринг-строку в `.claude/docs`.

Инструмент отвечает на два вопроса: что изменено по подсистемам (с последней
правкой файла — чтобы видеть чужое) и какие из стаженных модулей уходят в коммит
без своей строки в доках. Имена файлов берёт из git, ничего про проект не знает.
"""
import argparse
import subprocess
from pathlib import Path


def commit_scope_changes(root='.') -> list[dict]:
    """Изменённое в дереве по подсистемам: файл, статус, последняя правка.

    Args:
        root: корень git-дерева.

    Returns:
        [{'file', 'status', 'area', 'last'}, ...]: `area` — верхний слой пути
        (подсистема), `last` — последняя правка файла (`краткое имя: subject`),
        по ней видно чужое: своя правка здесь та, что ещё не в истории.
    """
    base = Path(root).resolve()
    done = subprocess.run(['git', 'status', '--porcelain'], cwd=str(base),
                          capture_output=True, text=True, timeout=10)
    out = []
    for line in (done.stdout or '').splitlines():
        status, where = line[:2], line[3:].split(' -> ')[-1].strip('"')
        out.append({'file': where, 'status': status.strip() or '?',
                    'area': Path(where).parts[0] if Path(where).parts else '',
                    'last': _last(base, where)})
    out.sort(key=lambda c: (c['area'], c['file']))
    return out


def commit_scope_guard(root='.') -> list[dict]:
    """Стаженные модули без своей строки в стаженных же доках — дыра в изменении.

    Args:
        root: корень git-дерева.

    Returns:
        [{'module', 'docs_touched'}]: изменённые `src/**.py`, чьё имя не
        встретилось ни в одном стаженном `.md` — док не едет в этом коммите.
    """
    base = Path(root).resolve()
    staged = subprocess.run(['git', 'diff', '--cached', '--name-only'],
                            cwd=str(base), capture_output=True, text=True,
                            timeout=10).stdout.splitlines()
    mods = {Path(s).stem for s in staged if s.endswith('.py')
            and Path(s).parts[0] == 'src'}
    docs = '\n'.join(_diff_cached(base, s) for s in staged
                     if s.endswith('.md'))
    return [{'module': m, 'docs_touched': False}
            for m in sorted(mods) if m not in docs]


# ── детали реализации ──

def _last(base: Path, where: str) -> str:
    """Последний коммит, трогавший файл: `имя: subject` — чужое видно сразу."""
    done = subprocess.run(['git', 'log', '-1', '--format=%an: %s', '--', where],
                          cwd=str(base), capture_output=True, text=True, timeout=10)
    return (done.stdout or '').strip()[:120]


def _diff_cached(base: Path, where: str) -> str:
    done = subprocess.run(['git', 'diff', '--cached', '--', where], cwd=str(base),
                          capture_output=True, text=True, timeout=10)
    return done.stdout or ''


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Что изменено по подсистемам и что ушло бы в коммит без дока.')
    ap.add_argument('--root', default='.')
    ns = ap.parse_args()
    last = ''
    for c in commit_scope_changes(ns.root):
        if c['area'] != last:
            print(f'\n— {c["area"]}')
            last = c['area']
        print(f'  [{c["status"]}] {c["file"]}  ← {c["last"]}')
    holes = commit_scope_guard(ns.root)
    for h in holes:
        print(f'  без строки дока в этом изменении: {h["module"]}')

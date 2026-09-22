"""
Панель бежит правку или ещё старый код: старт контейнера против изменённых файлов.

У контейнера панели нет `--reload`: файл на диске изменён, а процесс внутри
контейнера загрузил модуль на своём старте — тесты и запросы идут старому коду,
и правка «не работает», хотя работает. Плюс мёртвые `.pyc`: у удалённого модуля
компилированный байткод остаётся в `__pycache__` (часто root-овский, его руками
не снять), и следующий, кто импортирует имя, получает модуль, которого в дереве
уже нет.

Модуль отвечает на два вопроса: старше ли процесса панели самые свежие правки
(нужен рестарт — `uvicorn_dev_up`) и какие `.pyc` переживи свои модули.
"""
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

UVICORN_STALE_SKIP = ('.venv', '.git', 'node_modules')


def uvicorn_stale_check(root='.', service='uvicorn') -> dict:
    """Старее ли код запущенной панели и какие .pyc пережили свои модули.

    Args:
        root: корень проекта (git-дерево с изменёнными файлами).
        service: подстрока имени compose-сервиса панели.

    Returns:
        {'started': ISO время старта контейнера, 'stale': [изменённый файл,
        изменённый позже старта], 'dead_pyc': [*.pyc без своего .py рядом],
        'restart': bool — правки старше процесса нет, рестарт нужен}.
    """
    base = Path(root).resolve()
    started = _started_at(service)
    stale = []
    if started:
        for where in _changed(base):
            path = base / where
            if path.exists():
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if mtime > started:
                    stale.append(where)
    dead = []
    for cache in base.rglob('__pycache__'):
        if any(part in UVICORN_STALE_SKIP for part in cache.parts):
            continue
        for pyc in sorted(cache.glob('*.pyc')):
            stem = pyc.name.split('.')[0]
            if not (pyc.parent.parent / f'{stem}.py').exists():
                dead.append(pyc.relative_to(base).as_posix())
    return {'started': started.isoformat() if started else '',
            'stale': stale, 'dead_pyc': dead,
            'restart': bool(stale)}


# ── детали реализации ──

def _started_at(service: str):
    """Время старта контейнера сервиса; не поднят или нет docker — None."""
    try:
        names = subprocess.run(['docker', 'ps', '--format',
                                '{{.Names}}\t{{.ID}}'],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (names.stdout or '').splitlines():
        parts = line.split('\t')
        if len(parts) >= 2 and service in parts[0]:
            try:
                raw = subprocess.run(['docker', 'inspect', '-f',
                                      '{{.State.StartedAt}}', parts[1]],
                                     capture_output=True, text=True,
                                     timeout=10).stdout.strip()
                return datetime.fromisoformat(
                    raw.replace('Z', '+00:00')) if raw else None
            except (subprocess.SubprocessError, ValueError):
                return None
    return None


def _changed(base: Path) -> list:
    """Изменённые в git-дереве файлы без служебных каталогов."""
    done = subprocess.run(['git', 'status', '--porcelain'], cwd=str(base),
                          capture_output=True, text=True, timeout=10)
    out = []
    for line in (done.stdout or '').splitlines():
        where = line[3:].split(' -> ')[-1].strip('"')
        if not any(part in UVICORN_STALE_SKIP for part in Path(where).parts):
            out.append(where)
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Панель бежит правку или ещё старый код: рестарт нужен?')
    ap.add_argument('--root', default='.', help='корень проекта с изменённым кодом')
    ap.add_argument('--service', default='uvicorn')
    ns = ap.parse_args()
    r = uvicorn_stale_check(ns.root, ns.service)
    print(f'панель: с {r["started"] or "не поднята"}')
    for f in r['stale']:
        print(f'  старее процесса: {f}')
    for f in r['dead_pyc']:
        print(f'  мёртвый байткод: {f}')
    print('рестарт нужен: uvicorn_dev_up' if r['restart']
          else 'панель бежит последний код')
    # Код возврата 1 — «панель бежит старый код»: цикл правки читает его
    # и сам зовёт рестарт, не разбирая текст.
    raise SystemExit(1 if r['restart'] else 0)

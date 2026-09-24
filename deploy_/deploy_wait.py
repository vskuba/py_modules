"""
Дождаться выкладки и ответить: на проде уже новый код или ещё старый.

Выкладка идёт минутами, и всё это время всякая проверка на проде отвечает про
**старый** код. Отсюда два промаха, которые повторяются:

- смотришь журнал прогонов и видишь беду, которую только что починил, — а прогон
  был до выкладки;
- решаешь, что правка не работает, и начинаешь чинить уже починенное.

Различить это по времени нельзя: метки прода идут в его поясе, а твои часы — в
своём, и разница в три часа выглядит как «час назад». Различить можно только по
**коммиту**: он либо доехал, либо нет.

## Что делает

`deploy_wait_run()` ждёт, пока выкладка кончится, и говорит исход словами.
`deploy_wait_landed()` отвечает на главный вопрос — тот ли коммит сейчас на
проде; ей всё равно, что показывает CI, она спрашивает сам сервер.

## ⚠ Ответ «выкладка удалась» — не то же, что «код на проде»

Между зелёной галочкой CI и перезапущенным контейнером проходят секунды, а
иногда и минуты: образ выкладывается, старый процесс дорабатывает запрос. Оттого
`deploy_wait_landed` спрашивает не CI, а прод, и спрашивает про коммит.

## ⚠ Чего этот модуль не делает

Не выкладывает и не откатывает. Только смотрит — чтобы вывод «работает / не
работает» делался по тому, что вправду крутится.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# Как часто спрашивать CI. Реже — узнаёшь об отказе с опозданием, чаще — стучишь
# впустую: выкладка идёт минутами, а не секундами.
DEPLOY_WAIT_EVERY = 20

# Предел ожидания. Выкладка, идущая дольше получаса, — уже повод посмотреть на
# неё глазами, а не ждать дальше.
DEPLOY_WAIT_LIMIT = 1800


def deploy_wait_run(run_id: str = '', repo_dir: str = '.',
                    every: int = DEPLOY_WAIT_EVERY,
                    limit: int = DEPLOY_WAIT_LIMIT,
                    workflow: str = '') -> dict:
    """
    Дождаться окончания выкладки.

    Args:
        run_id: номер прогона; пусто — последний по текущей ветке.
        repo_dir: каталог репозитория.
        every: как часто спрашивать, секунд.
        limit: сколько ждать всего, секунд.
        workflow: кусок имени нужного CI-процесса. ⚠ Почти обязателен: на толчок
            отвечают несколько (сама выкладка и уведомление о её исходе), и
            «последний» оказывается коротким уведомлением — оно кончается первым,
            и ожидание завершается до выкладки.
            ⚠⚠ Кусок должен быть **различающим**: `deploy` находит и
            «Deploy to Oracle Cloud», и «On Deploy Status». Берите то, чего у
            соседа нет: `oracle`, `cloud`.

    Returns:
        `{'id', 'status', 'conclusion', 'title', 'waited'}`.

    Raises:
        RuntimeError: `gh` недоступен или прогон не найден.

    ⚠ Ждём **по номеру**, а не «последний каждый раз»: пока ждёшь, может уехать
    ещё один толчок, и ожидание незаметно переедет на чужую выкладку.
    """
    run_id = str(run_id or '') or _last_run(repo_dir, workflow)
    started = time.monotonic()

    while True:
        got = _run_view(run_id, repo_dir)
        waited = int(time.monotonic() - started)

        if got.get('status') == 'completed':
            return {**got, 'id': run_id, 'waited': waited}

        if waited >= limit:
            return {**got, 'id': run_id, 'waited': waited,
                    'conclusion': 'таймаут ожидания'}

        time.sleep(max(1, int(every)))


def deploy_wait_landed(commit: str, probe: str) -> dict:
    """
    Тот ли коммит сейчас на проде.

    Args:
        commit: ожидаемый коммит (хватит первых семи знаков).
        probe: команда, печатающая коммит, который крутится на проде.
            Например: `ssh хост 'cd ~/app && git rev-parse HEAD'`.

    Returns:
        `{'want', 'got', 'landed'}` — `landed` отвечает на вопрос.

    ⚠ Спрашиваем **прод**, а не CI: зелёная галочка означает «выкладка прошла», а
    не «процесс перезапущен». Между ними секунды, и именно в них рождается вывод
    «правка не работает».
    """
    want = str(commit or '').strip()
    if not want:
        raise RuntimeError('нужен коммит, который ждём')

    got = _shell(probe).strip().splitlines()
    here = got[-1].strip() if got else ''
    short = min(len(want), len(here), 7)

    return {'want': want, 'got': here,
            'landed': bool(here) and here[:short] == want[:short]}


def deploy_wait_format(run: dict, landed: dict = None) -> str:
    """Исход словами: что с выкладкой и что на проде."""
    mark = {'success': '✓ удалась', 'failure': '✗ УПАЛА',
            'cancelled': '⊘ отменена'}.get(str(run.get('conclusion')),
                                           str(run.get('conclusion') or '—'))
    lines = [f"выкладка #{run.get('id')}: {mark}"
             f"  ({run.get('waited', 0)} с, «{str(run.get('title') or '')[:48]}»)"]

    if landed:
        lines.append('на проде: ' + ('тот самый коммит' if landed.get('landed')
                                     else f"ЕЩЁ СТАРЫЙ ({landed.get('got') or '?'})"))
        if not landed.get('landed'):
            lines.append('⚠ Проверять поведение рано: крутится прежний код.')

    return '\n'.join(lines)


def _last_run(repo_dir: str, workflow: str = '') -> str:
    """Номер последнего подходящего прогона.

    ⚠ Берём десяток и отбираем по имени: на один толчок отвечают несколько
    процессов, и «самый последний» — обычно короткое уведомление, а не выкладка.
    """
    raw = _shell('gh run list --limit 10 --json databaseId,workflowName,name',
                 repo_dir)
    try:
        got = json.loads(raw or '[]')
    except ValueError as err:
        raise RuntimeError(f'gh ответил не JSON: {raw[:120]}') from err

    want = str(workflow or '').lower()
    for row in got:
        title = f"{row.get('workflowName') or ''} {row.get('name') or ''}".lower()
        if not want or want in title:
            return str(row['databaseId'])

    raise RuntimeError(f'прогон не найден: {workflow or "любой"}')


def _run_view(run_id: str, repo_dir: str) -> dict:
    """Состояние прогона по номеру."""
    raw = _shell(f'gh run view {int(run_id)} '
                 f'--json status,conclusion,displayTitle', repo_dir)
    try:
        got = json.loads(raw or '{}')
    except ValueError as err:
        raise RuntimeError(f'gh ответил не JSON: {raw[:120]}') from err

    return {'status': got.get('status'), 'conclusion': got.get('conclusion'),
            'title': got.get('displayTitle')}


def _shell(command: str, cwd: str = None) -> str:
    """Команда оболочки.

    ⚠ `shell=True` намеренно: сюда приходит команда оператора с ssh и кавычками
    в три слоя (`--probe`). Строка берётся из своей же командной строки, чужого
    ввода здесь нет. Номер прогона при этом приводится к `int` — он-то приходит
    из ответа `gh`.
    """
    got = subprocess.run(command, shell=True, capture_output=True,
                         text=True, timeout=300,
                         cwd=str(cwd) if cwd else None)
    if got.returncode and not got.stdout.strip():
        raise RuntimeError(f'{command[:60]}…: {got.stderr.strip()[:200]}')

    return got.stdout


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Дождаться выкладки и сказать, доехал ли коммит до прода.')
    parser.add_argument('--run', default='', help='номер прогона; пусто — последний')
    parser.add_argument('--dir', default='.', help='каталог репозитория')
    parser.add_argument('--commit', default='',
                        help='коммит, которого ждём на проде; пусто — HEAD')
    parser.add_argument('--probe', default='',
                        help='команда, печатающая коммит с прода')
    parser.add_argument('--workflow', default='',
                        help='кусок имени нужного CI-процесса: на толчок '
                             'отвечают несколько, и «последний» бывает не тем')
    parser.add_argument('--every', type=int, default=DEPLOY_WAIT_EVERY)
    parser.add_argument('--limit', type=int, default=DEPLOY_WAIT_LIMIT)
    args = parser.parse_args()

    try:
        run = deploy_wait_run(args.run, args.dir, args.every,
                              args.limit, args.workflow)
        landed = None

        if args.probe:
            want = args.commit or _shell('git rev-parse HEAD', args.dir).strip()
            landed = deploy_wait_landed(want, args.probe)

        print(deploy_wait_format(run, landed))
        raise SystemExit(0 if run.get('conclusion') == 'success' else 1)
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

"""
Чем кончился последний пайплайн ветки: упавшая джоба, причина из живого лога.

С 2026-11 лог джобы доступен API (`circleci_api_job_log`), поэтому диагноз
строится по меткам вывода, а не по догадкам:

* behat: блок `--- Failed scenarios:` со списком сценариев и итог `N failed`;
* phpunit: `FAILURES!`/`Failed asserting` с именем упавшего теста;
* сборка: `E: Failed to fetch`, `Temporary failure resolving` — сеть при apt;
* ничего из этого: последние строки лога как они есть.

Длительность джобы осталась запасной подсказкой — только когда лог недостижим.
Карта по длительности (типовой проект): ≤1 мин — встала до сборки; 2-4 мин —
apt/загрузки base; 5-8 мин — глубже Dockerfile; дальше — сами тесты. У deploy-
джобы минуты тишины перед провалом — `helm --wait`, толчок не прошёл readiness.
"""
import argparse
import re
from datetime import datetime

from circleci_.circleci_api import (circleci_api_job, circleci_api_job_log,
                                    circleci_api_latest_jobs, circleci_api_slug)
from circleci_.circleci_watch import circleci_watch_probe

# Порог «джоба не начала ничего»: меньше — встал до первого шага.
CIRCLECI_BLAME_BIRTH = 60
# Метки провала в консольном потоке: (кто, коронные фразы).
CIRCLECI_BLAME_MARKS = (
    ('behat', ('--- Failed scenarios:',)),
    ('phpunit', ('FAILURES!', 'Failed asserting')),
    ('apt/сеть', ('E: Failed to fetch', 'Temporary failure resolving',
                  'Could not get', 'Unable to fetch')),
)


def circleci_blame_latest(slug: str = '', branch: str = 'dev') -> dict:
    """Разбор последнего пайплайна ветки.

    Args:
        slug: `gh/org/repo`; пусто — из git-remote.
        branch: ветка.

    Returns:
        `{'number', 'state', 'revision', 'status' (по workflow), 'failed':
        [{name, job_number, id, seconds, guess}], 'jobs': {имя: статус}}`;
        `guess` — догадка по длительности (см. карту в docstring), `''` у
        успевших.
    """
    slug = slug or circleci_api_slug()
    got = circleci_api_latest_jobs(slug, branch)
    if not got:
        return {'number': None, 'failed': [], 'jobs': {}}
    failed, jobs = [], {}
    status = 'success'
    for w in got['workflows'].values():
        if w['status'] != 'success':
            status = w['status']
        for jn, j in w['jobs'].items():
            jobs[jn] = j['status']
            if j['status'] not in ('failed', 'errored', 'canceled'):
                continue
            seconds = _seconds(j['id'])
            entry = {'name': jn, 'job_number': j['job_number'],
                     'id': j['id'], 'seconds': seconds,
                     'guess': _guess(seconds, jn)}
            entry.update(circleci_blame_failure(slug, j['job_number'], jn))
            failed.append(entry)
    return {'number': got['number'], 'state': got['state'],
            'revision': got['revision'], 'status': status,
            'failed': failed, 'jobs': jobs}


def circleci_blame_failure(slug: str, job_number: int, name: str = '') -> dict:
    """Причина провала джобы из живого лога.

    Returns:
        `{'cause': строка, 'evidence': [строки], 'from_log': bool}`; при
        недоступном логе `cause` — догадка по длительности, `from_log=False`.
    """
    if 'deploy' in name:
        # у deploy-лог короткий и специфичный (helm/kubectl), метки тестов не ждутся
        return {'cause': '', 'evidence': [], 'from_log': False}
    try:
        log = circleci_api_job_log(slug, job_number)
    except Exception:
        return {'cause': '', 'evidence': [], 'from_log': False}
    lines = [l.strip() for l in log.splitlines() if l.strip()]
    for who, marks in CIRCLECI_BLAME_MARKS:
        if not any(m in log for m in marks):
            continue
        if who == 'behat':
            return {'cause': 'behat: ' + _behat_tally(log),
                    'evidence': _behat_scenarios(log), 'from_log': True}
        if who == 'phpunit':
            return {'cause': 'phpunit: падения',
                    'evidence': _dedup([l for l in lines if 'Failed asserting' in l
                                        or re.match(r'^\d+\)', l)])[:4],
                    'from_log': True}
        return {'cause': 'сборка: сеть при загрузке пакетов',
                'evidence': _dedup([l for l in lines
                                    if 'Failed to fetch' in l or 'resolving' in l])[:4],
                'from_log': True}
    return {'cause': 'меток нет — последние строки лога',
            'evidence': lines[-3:], 'from_log': True}


def _behat_tally(log: str) -> str:
    """Итог behat одной строкой: `2 из 36 сценариев`. Метка без итога — 'упали сценарии'."""
    m = re.search(r'(\d+) scenarios \((\d+) passed, (\d+) failed\)', log)
    return f'{m.group(3)} из {m.group(1)} сценариев' if m else 'упали сценарии'


def _behat_scenarios(log: str) -> list:
    """Список из блока `--- Failed scenarios:` (файл:строка на сценарий).

    Строки блока идут в контейнерном префиксе (`fpm |  features/...:72 ...`),
    поэтому не `startswith`, а поиск по куску лога после метки: до него
    сценарии мелькают в комментариях шагов и их брать нельзя.
    """
    _, _, tail = log.partition('--- Failed scenarios:')
    tail = tail.partition(' scenarios (')[0]
    return _dedup(re.findall(r'features/\S+?\.feature:\d+(?: \(on line \d+\))?', tail))[:8]


def _dedup(lines: list) -> list:
    seen, out = set(), []
    for l in lines:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def _seconds(job_id: str) -> int:
    """Длительность джобы, сек; 0 — времен не удалось достать."""
    try:
        j = circleci_api_job(job_id)
        a = datetime.fromisoformat(j['started_at'].replace('Z', '+00:00'))
        b = datetime.fromisoformat(j['stopped_at'].replace('Z', '+00:00'))
        return max(0, int((b - a).total_seconds()))
    except Exception:
        return 0


def _guess(seconds: int, name: str = '') -> str:
    """Догадка по длительности — карта из docstring; для deploy-джоб своя фаза."""
    if 'deploy' in name:
        if seconds < CIRCLECI_BLAME_BIRTH:
            return 'деплой встал до действий: доступ/контекст кластера'
        if seconds < 600:
            return ('деплой отработал и отменился: новый под не поднялся '
                    '(readiness/liveness) или шаг после push не прошёл')
        return 'деплой висел на rollout-таймауте — смотреть поды стенда'
    if seconds <= CIRCLECI_BLAME_BIRTH:
        return 'встала до сборки: конфиг/авторизация'
    if seconds < 260:
        return 'сборка образа сорвалась на сетевом слое (apt/загрузки)'
    if seconds < 500:
        return 'сборка сорвалась глубже (зависимости, скрипты образа)'
    return 'провал после сборки — тесты'


def _cli(argv=None):
    ap = argparse.ArgumentParser(description='Что упало в последнем пайплайне ветки.')
    ap.add_argument('--slug', default='')
    ap.add_argument('--branches', default='dev,staging')
    a = ap.parse_args(argv)
    print(circleci_watch_probe(a.slug, tuple(b.strip() for b in a.branches.split(','))))
    for br in (b.strip() for b in a.branches.split(',')):
        got = circleci_blame_latest(a.slug, br)
        for f in got.get('failed', []):
            why = f.get('cause') or (f"догадка: {f['guess']}" if f['guess'] else '')
            print(f"  {br} #{got['number']} {f['name']}: {f['seconds']}с — {why}"
                  if why else
                  f"  {br} #{got['number']} {f['name']}: {f['seconds']}с")
            for e in f.get('evidence', []):
                print(f'      {e}')


if __name__ == '__main__':
    _cli()

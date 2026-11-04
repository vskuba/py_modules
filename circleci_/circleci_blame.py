"""
Чем кончился последний пайплайн ветки: упавшая джоба, её длительность, догадка.

Лога CircleCI через API не даёт (см. карту в `circleci_api`), и без этого вопроса
ответ «упало» бесполезен: непонятно, куда смотреть. Дельта этого модуля —
длительность джобы как указатель. Длительность — единственное, что API о провале
говорит честно, а фазы сборки на типовом проекте занимают предсказуемо:

    ~10 сек   образ даже не начал собираться — конфиг/авторизация
    ~2-4 мин  образ собирался до первого сетевого шага — apt/загрузки base
    ~5-8 мин  провал глубже Dockerfile — composer install, тесты образа
    дальше    сами тесты

А у deploy-джобы фазы другие и другая же логика: минуты тишины перед провалом
там — это `helm --wait`, то есть толчок не прошёл readiness, а не скрипт упал.

Ручной ритуал, который заменяет модуль: открыть пайплайн глазами, найти красную
джобу, сравнить её sha с головой ветки (а не чужой прошлый прогон принять за
свой — классический обман на два параллельных push), засечь длительность.

⚠ Догадка по длительности — догадка, не диагноз. Диагноз дают `Rerun Job with
SSH` (строку ssh даёт человек в UI джобы) и локальное воспроизведение шага.
"""
import argparse
from datetime import datetime

from circleci_.circleci_api import circleci_api_job, circleci_api_latest_jobs, circleci_api_slug
from circleci_.circleci_watch import circleci_watch_probe

# Порог «джоба не начала ничего»: меньше — встал до первого шага.
CIRCLECI_BLAME_BIRTH = 60


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
            failed.append({'name': jn, 'job_number': j['job_number'],
                           'id': j['id'], 'seconds': seconds,
                           'guess': _guess(seconds, jn)})
    return {'number': got['number'], 'state': got['state'],
            'revision': got['revision'], 'status': status,
            'failed': failed, 'jobs': jobs}


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
            print(f"  {br} #{got['number']} {f['name']}: {f['seconds']}с — {f['guess']}"
                  if f['guess'] else
                  f"  {br} #{got['number']} {f['name']}: {f['seconds']}с")


if __name__ == '__main__':
    _cli()

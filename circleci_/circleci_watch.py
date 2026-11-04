"""
Дождаться CI-выкладки ветки и одним взглядом увидеть, чем кончилась.

`deploy_.deploy_wait` стережёт GitHub Actions и спрашивает сам сервер; здесь та
же задача для CircleCI, где сервера спросишь не всегда (k8s, чужие стенды), а
источник правды — пайплайн: push в ветку, и через минуты ясно, дошли ли
`build → tests → deploy` или встали.

Два способа смотреть, для двух ритмов:

* `circleci_watch_probe` — **один** снимок словами (`dev#687
  build-n-deploy=failed deploy-dev=failed ...`). Годится вслепую: его строкой
  зовут сторожа (sentinel/command, interval ~60) — тот спит между заходами и
  будит только на перемену. Вручную — то же: глянул и ушёл.
* `circleci_watch_branch` — сидит и ждёт терминального состояния, вернёт разбор
  по джобам. Для «запушил и жду ответа».

⚠ Статусы пайплайна и workflow перемешаны по времени: workflow бывает `running`,
когда его уже упавшие джобы расставлены — терминальным считают себя сам
workflow, джобы читают как есть.
⚠ Не путай с `deploy_.deploy_wait`: тот про «доехал ли код на сервер», этот
про «что решил CI». Зелёный CI ещё не значит «прод живой» — за зелёным
смотри deploy_wait/deploy_proof.
"""
import argparse
import sys
import time

from circleci_.circleci_api import circleci_api_latest_jobs, circleci_api_slug

# Реже — узнаёшь об отказе с опозданием в лишний шаг; чаще — стучишь впустую:
# пайплайн идёт минутами.
CIRCLECI_WATCH_EVERY = 30

# Долше получаса ждать не стоит: висящий workflow — повод смотреть глазами, а
# не экраном.
CIRCLECI_WATCH_LIMIT = 1800

# Конченые состояния workflow. `on_hold` — ждёт approvals, не кончено.
CIRCLECI_WATCH_FINAL = ('success', 'failed', 'errored', 'canceled', 'not_run')


def circleci_watch_probe(slug: str = '', branches=('dev', 'staging')) -> str:
    """Снимок последних пайплайнов веток — по строке на ветку.

    Формат строки ровно тот, на чём удобно ловить сторожа перемену и регуляркой
    `build-n-deploy=(success|failed)`:

        dev#687 build-n-deploy=failed deploy-dev=failed app-tests-dev=success
        staging#685 build-n-deploy=running deploy-stage=not_run

    Args:
        slug: `gh/org/repo`; пусто — из git-remote каталога.
        branches: ветки по порядку.

    Returns:
        Текст снимка; пустых веток (ни пайплайна) не бывает — там `—`.
    """
    slug = slug or circleci_api_slug()
    lines = []
    for br in branches:
        got = circleci_api_latest_jobs(slug, br)
        if not got:
            lines.append(f'{br} —')
            continue
        parts = []
        for name, w in got['workflows'].items():
            parts.append(f"{name}={w['status']}")
            for jn, j in w['jobs'].items():
                parts.append(f"{jn}={j['status']}")
        lines.append(f"{br}#{got['number']} " + ' '.join(parts))
    return '\n'.join(lines)


def circleci_watch_branch(slug: str = '', branch: str = 'dev',
                          every: int = CIRCLECI_WATCH_EVERY,
                          limit: int = CIRCLECI_WATCH_LIMIT,
                          expect_sha: str = '') -> dict:
    """Дождаться терминального состояния последнего пайплайна ветки.

    Args:
        slug: `gh/org/repo`; пусто — из git-remote.
        branch: ветка.
        every: пауза, сек.
        limit: предел, сек; за него — статус `таймаут`.
        expect_sha: какой revision ждёшь; чужой (например, CI ещё не поднял
            новый push) ждётся молча — ждём именно свой, а не «последний
            когда-нибудь».

    Returns:
        Разбор `circleci_api_latest_jobs` плюс `waited` и `status`
        (`success`/`failed`/... — по первому workflow; `таймаут`).
    """
    slug = slug or circleci_api_slug()
    started = time.monotonic()
    last: dict = {}
    while True:
        last = circleci_api_latest_jobs(slug, branch)
        waited = int(time.monotonic() - started)
        if last and (not expect_sha or last['revision'].startswith(expect_sha)):
            statuses = [w['status'] for w in last['workflows'].values()]
            if statuses and all(s in CIRCLECI_WATCH_FINAL for s in statuses):
                last['waited'] = waited
                last['status'] = ('success' if all(s == 'success' for s in statuses)
                                  else 'failed')
                return last
        if waited >= limit:
            last = {**last, 'waited': waited, 'status': 'таймаут'}
            return last
        time.sleep(every)


def _cli(argv=None):
    ap = argparse.ArgumentParser(description='Снимок или ожидание CI-пайплайнов CircleCI.')
    ap.add_argument('--slug', default='')
    ap.add_argument('--branches', default='dev,staging', help='для --probe')
    ap.add_argument('--branch', default='dev', help='для ожидания')
    ap.add_argument('--sha', default='', help='ждать пайплайна этой ревизии')
    ap.add_argument('--probe', action='store_true',
                    help='один снимок; код 0 всё success, 4 есть failed, 3 ещё идёт')
    a = ap.parse_args(argv)

    if a.probe:
        text = circleci_watch_probe(a.slug, tuple(b.strip() for b in a.branches.split(',')))
        print(text)
        if '=failed' in text or '=errored' in text:
            sys.exit(4)
        if any(w in text for w in ('=running', '=on_hold', '=queued')):
            sys.exit(3)
        sys.exit(0)

    got = circleci_watch_branch(a.slug, a.branch, expect_sha=a.sha)
    print(f"пайплайн #{got.get('number')} {got.get('status', '')} "
          f"({got.get('waited', 0)}с) rev {got.get('revision', '')[:7]}")
    for name, w in got.get('workflows', {}).items():
        print(f'  {name}: {w["status"]}')
        for jn, j in w['jobs'].items():
            print(f'    {jn}: {j["status"]} #{j["job_number"]}')
    sys.exit(0 if got.get('status') == 'success' else 4)


if __name__ == '__main__':
    _cli()

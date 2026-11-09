"""
CircleCI API v2: пайплайны, workflow, джобы — без ручного тыка в эндпоинты.

Модуль родился на выладке, где добыть «почему упало» руками стоило часов тыка:
ловушки API не описаны так, чтобы найтись с ходу, а выглядят как сломанный
эндпоинт. Карта ловушек — в docstring, она и есть половина ценности модуля.

## Карта API (проверено на живом проекте, 2026-11)

* **Логи — через карточку v1.1, не через v2.** У v2 нет эндпоинтов вывода
  (`openapi.json` молчит), и это ввёл в заблуждение: казалось, логов нет вовсе.
  Но `/api/v1.1/project/{slug}/{номер}` отвечает на **тот же CCIPAT-токен**
  (заголовок `Circle-Token`), а в карточке у каждого шага есть
  `actions[0].output_url` — presigned-адрес потока консоли (`…/api/private/
  output/presigned/…`), его тело — JSON-массив чанков `{"message": …}`.
  Это и есть полный лог джобы: `circleci_api_job_log`. Раньше верили, что
  путь к логу один — `Rerun Job with SSH` руками человека; теперь это только
  для интерактивной отладки.
* **workflow спрашивает по uuid пайплайна, не по номеру**: `/pipeline/687/...`
  отвечает «Invalid pipeline ID» — и это выглядит как сломанный эндпоинт, а
  это номер вместо uuid.
* **GraphQL (`circleci.com/graphql`) — тупик**: отвечает без авторизации,
  интроспекции нет, полей пайплайнов/джоб под токеном не найдено. Не трать время.
* Цепочка, которая работает: `pipeline?branch=` → (uuid) `pipeline/{uuid}/workflow`
  → (uuid) `workflow/{uuid}/job` → (uuid) `jobs/{uuid}`.

## Что здесь есть

* `circleci_api_token` — токен из окружения или `~/.config/circleci_token`.
* `circleci_api_pipelines` — пайплайны проекта, можно фильтром по ветке.
* `circleci_api_workflows` — workflow пайплайна (по uuid).
* `circleci_api_jobs` — джобы workflow с номерами, статусами и uuid.
* `circleci_api_job` — детали одной джобы (времена; логов нет — см. карту).
* `circleci_api_job_log` — полный лог джобы: карточка v1.1 → output_url шага.
* `circleci_api_latest_jobs` — вся цепочка до джоб последнего пайплайна ветки.

CLI:

    python -m circleci_.circleci_api pipelines --slug gh/org/repo --branch dev
    python -m circleci_.circleci_api jobs --slug gh/org/repo --branch dev
    python -m circleci_.circleci_api jobs --workflow <uuid-workflow>
    python -m circleci_.circleci_api job-log --job 3957 --tail 40

⚠ `slug` — это `gh/<org>/<repo>` (или git-remote из текущего каталога, если
каталог — клон того же проекта).
"""
import argparse
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

BASE = 'https://circleci.com/api/v2'
BASE_V1 = 'https://circleci.com/api/v1.1/project'


def circleci_api_token() -> str:
    """Токен CircleCI: окружение `CIRCLE_TOKEN`, иначе `~/.config/circleci_token`.

    Raises:
        RuntimeError: токена нет ни там, ни там.
    """
    tok = os.environ.get('CIRCLE_TOKEN', '').strip()
    if tok:
        return tok
    path = os.path.expanduser('~/.config/circleci_token')
    if os.path.exists(path):
        return open(path).read().strip()
    raise RuntimeError('CircleCI-токена нет: задай CIRCLE_TOKEN или ~/.config/circleci_token')


def circleci_api_slug(repo_dir: str = '.') -> str:
    """Слаг проекта `gh/org/repo` из git-remote текущего каталога.

    Raises:
        RuntimeError: каталог не git или remote без github.com.
    """
    try:
        url = subprocess.run(['git', '-C', repo_dir, 'remote', 'get-url', 'origin'],
                             capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        raise RuntimeError(f'не git-клон: {repo_dir}')
    for sep in ('github.com:', 'github.com/'):
        if sep in url:
            tail = url.split(sep, 1)[1].removesuffix('.git').strip('/')
            if '/' in tail:
                return 'gh/' + tail
    raise RuntimeError(f'в remote нет github.com: {url}')


def circleci_api_get(path: str, params: dict | None = None,
                     token: str | None = None, timeout: float = 15,
                     base: str = BASE):
    """GET к API (v2 по умолчанию); возвращает разобранный JSON.

    Args:
        path: путь после версии, напр. `project/gh/org/repo/pipeline`.
        params: query-параметры, пустые отбрасываются.
        token: перебивает `circleci_api_token`.
        base: префикс версии; `BASE_V1` для карточек v1.1.

    Raises:
        RuntimeError: HTTP-ошибка — текст ответа в сообщении.
    """
    tok = token or circleci_api_token()
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, '')})
    url = f'{base}/{path}' + (f'?{query}' if query else '')
    req = urllib.request.Request(url, headers={'Circle-Token': tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f'{err.code} {url}: {err.read().decode(errors="replace")[:200]}')


def circleci_api_pipelines(slug: str, branch: str = '', page_size: int = 20,
                           page_token: str = '') -> dict:
    """Пайплайны проекта, свежие сверху.

    Args:
        slug: `gh/org/repo`.
        branch: фильтр по ветке; пусто — все.
        page_size: страницей до 100.
        page_token: продолжение страницы из прошлого ответа.

    Returns:
        `{'items': [...], 'next_page_token': ...}`; в items — `id` (uuid),
        `number`, `state`, `vcs.revision`, `vcs.branch`.
    """
    return circleci_api_get(f'project/{slug}/pipeline',
                            {'branch': branch, 'page-size': page_size,
                             'page-token': page_token})


def circleci_api_workflows(pipeline_id: str) -> list:
    """Workflow пайплайна: `name`, `status`, `id` (uuid для `circleci_api_jobs`).

    Raises:
        RuntimeError: передан номер пайплайна вместо uuid («Invalid pipeline ID»).
    """
    return circleci_api_get(f'pipeline/{pipeline_id}/workflow')['items']


def circleci_api_jobs(workflow_id: str) -> list:
    """Джобы workflow: `name`, `job_number`, `status`, `id` (uuid для `circleci_api_job`)."""
    return circleci_api_get(f'workflow/{workflow_id}/job')['items']


def circleci_api_job(job_id: str) -> dict:
    """Детали джобы по uuid: времена `started_at`/`stopped_at`, статус. Лога тут нет."""
    return circleci_api_get(f'jobs/{job_id}')


def circleci_api_job_log(slug: str, job_number: int, step: str = '',
                         all_steps: bool = False) -> str:
    """Полный консольный лог джобы, очищенный от ANSI-кодов и `\\r`.

    Путь: карточка v1.1 (`project/{slug}/{номер}`) → шаги → `output_url`
    экшенов → JSON-чанки `{"message": ...}`. По умолчанию берётся упавший шаг
    (exit_code != 0) или, если он не опознан, шаги с логом целиком; `step` —
    подстрока имени шага, `all_steps` — собрать всё.

    Raises:
        RuntimeError: карточка или поток шага недоступны.
    """
    card = circleci_api_get(f'{slug}/{job_number}', base=BASE_V1, timeout=30)
    steps = card.get('steps', [])
    chosen = []
    for s_ in steps:
        if step and step not in (s_.get('name') or ''):
            continue
        acts = s_.get('actions') or []
        # без указания шага берём только упавший; пустой exit_code — шаг ещё
        # не отчитался, это не провал
        if all_steps or step or any(a.get('exit_code') for a in acts):
            chosen.append((s_, acts))
    if not chosen and not step:
        chosen = [(s_, s_.get('actions') or []) for s_ in steps]
    parts = []
    for s_, acts in chosen:
        name = s_.get('name') or ''
        for a in acts:
            url = a.get('output_url')
            if not url:
                continue
            req = urllib.request.Request(url, headers={'Circle-Token': circleci_api_token()})
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode('utf-8', 'replace')
            try:
                chunks = json.loads(body)
                body = ''.join(c.get('message', '') for c in chunks)
            except (json.JSONDecodeError, AttributeError):
                pass
            parts.append(f'===== шаг: {name} =====\n' + _clean(body))
    if not parts:
        raise RuntimeError(f'шаг {step!r} без лога в джобе {job_number}')
    return '\n'.join(parts)


def _clean(text: str) -> str:
    """Консольный поток к читаемому: ANSI, `\\r` как в `docker compose`."""
    text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', text)
    return text.replace('\r\n', '\n').replace('\r', '')


def circleci_api_latest_jobs(slug: str, branch: str) -> dict:
    """Джобы последнего пайплайна ветки разом — вся цепочка одним вызовом.

    Returns:
        `{'number', 'state', 'revision', 'workflows': {имя: {'status',
        'jobs': {имя: {'status', 'job_number', 'id'}}}}}`;
        пустой dict, если у ветки нет ни одного пайплайна.
    """
    got = circleci_api_pipelines(slug, branch=branch, page_size=10)
    # фильтр по ветке доверять нельзя: старые инстансы отвечали без него —
    # перестраховываемся сверкой на месте
    for p in got['items']:
        if branch and p.get('vcs', {}).get('branch') != branch:
            continue
        out = {'number': p['number'], 'state': p['state'],
               'revision': p.get('vcs', {}).get('revision', ''), 'workflows': {}}
        for w in circleci_api_workflows(p['id']):
            out['workflows'][w['name']] = {
                'status': w['status'],
                'jobs': {j['name']: {'status': j['status'],
                                     'job_number': j['job_number'],
                                     'id': j['id']}
                         for j in circleci_api_jobs(w['id'])},
            }
        return out
    return {}


def _cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('cmd', choices=['pipelines', 'workflows', 'jobs', 'job-log'])
    ap.add_argument('--slug', default='', help='gh/org/repo; пусто — из git-remote каталога')
    ap.add_argument('--branch', default='')
    ap.add_argument('--pipeline', default='', help='uuid пайплайна (не номер)')
    ap.add_argument('--workflow', default='', help='uuid workflow')
    ap.add_argument('--limit', type=int, default=10)
    ap.add_argument('--job', type=int, default=0, help='номер джобы для job-log')
    ap.add_argument('--step', default='', help='подстрока имени шага')
    ap.add_argument('--tail', type=int, default=0, help='последние N строк лога')
    a = ap.parse_args(argv)
    slug = a.slug or circleci_api_slug()

    if a.cmd == 'job-log':
        if not a.job:
            raise SystemExit('--job <номер> обязателен')
        log = circleci_api_job_log(slug, a.job, step=a.step, all_steps=a.step == '')
        if a.tail:
            log = '\n'.join(log.splitlines()[-a.tail:])
        print(log)
        return
    if a.cmd == 'pipelines':
        for p in circleci_api_pipelines(slug, branch=a.branch,
                                        page_size=a.limit)['items']:
            v = p.get('vcs', {})
            print(f"{p['number']} {v.get('branch') or v.get('tag') or '?'} "
                  f"{p['state']} {v.get('revision', '')[:7]} {p['id']}")
    elif a.cmd == 'workflows':
        if not a.pipeline:
            raise SystemExit('--pipeline <uuid> обязателен')
        for w in circleci_api_workflows(a.pipeline):
            print(f"{w['name']} {w['status']} {w['id']}")
    else:
        wid = a.workflow
        if not wid:
            got = circleci_api_latest_jobs(slug, a.branch)
            if not got:
                raise SystemExit(f'у ветки {a.branch} нет пайплайнов')
            print(f"пайплайн #{got['number']} {got['state']} {got['revision'][:7]}")
            for name, w in got['workflows'].items():
                for jn, j in w['jobs'].items():
                    print(f"  {jn} {j['status']} #{j['job_number']} {j['id']}")
            return
        for j in circleci_api_jobs(wid):
            print(f"{j['name']} {j['status']} #{j['job_number']} {j['id']}")


if __name__ == '__main__':
    _cli()

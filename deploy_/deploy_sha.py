"""Выкладка статики на surge и сверка по хешу: то, что легло, — то, что отдаётся.

`deploy_wait` и `deploy_proof` стерегут живой бэкенд; у статического сайта
(разведочная карта, отчёт) вопрос другой и короче: **доехал ли файл байт в
байт**. Руками это всегда одинаковая цепочка на семь строк: `npx surge` с
обходом npm-кэша, затем curl и sha256 локального против отдаваемого — и
промах «выложил, а отдаётся старое по кэшу» ловится только этим сравнением.
Здесь цепочка — одним вызовом; сверка идёт по всем файлам каталога, а не
только по index.

## Что здесь есть

* `deploy_sha_push` — выложить каталог на surge и сверить sha256 каждого
  файла с отдаваемым.

⚠ Surge отдаёт gzip'ом — качать надо с `Accept-Encoding` и сравнивать
декодированное; голое сравнение сырых байтов враньё на текстовых файлах.
⚠ Чужой домен во власти чужой песочницы: не совпал хеш у одного файла —
смотри, не тронул ли его хостинг (favicon, 404-страница).
"""
import hashlib
import os
import subprocess

import httpx


def deploy_sha_push(directory: str, host: str, *, timeout: float = 60.0) -> dict:
    """Выложить каталог на surge и сверить sha256 отданного с локальным.

    Args:
        directory: локальный каталог (плоский или с подпапками).
        host: домен выкладки (`wt24-intel.surge.sh`).
        timeout: секунд на fetch каждого файла.

    Returns:
        {'ok': выложен и всё совпало, 'pushed': вывод surge словами,
         'files': [{'file','local','served'|'', 'match': bool,
                    'status': http-код|0}],
         'error': словами (пусто при удаче)}
    """
    pushed, err = _sha_surge(directory, host)
    if err:
        return {'ok': False, 'pushed': pushed, 'files': [], 'error': err}
    files = []
    for root, _, names in os.walk(directory):
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, directory).replace(os.sep, '/')
            local = hashlib.sha256(open(full, 'rb').read()).hexdigest()
            served, status = _sha_fetch(host, rel, timeout)
            files.append({'file': rel, 'local': local, 'served': served,
                          'status': status,
                          'match': bool(served) and served == local})
    bad = [f for f in files if not f['match']]
    return {'ok': bool(files) and not bad, 'pushed': pushed, 'files': files,
            'error': '' if not bad else
            'не совпало: ' + ', '.join(f['file'] for f in bad)}


def _sha_surge(directory: str, host: str) -> tuple:
    """Один запуск npx surge; вывод хвостом, ошибка — словом."""
    env = dict(os.environ)
    cache = os.path.join(os.path.dirname(os.path.abspath(directory)),
                         '.npmcache')
    env['npm_config_cache'] = cache
    os.makedirs(cache, exist_ok=True)
    try:
        r = subprocess.run(['npx', '--yes', 'surge', directory, host],
                           capture_output=True, text=True, timeout=180, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return '', f'{type(e).__name__}'
    out = (r.stdout or '') + (r.stderr or '')
    if r.returncode != 0:
        return out[-300:], f'surge вернул {r.returncode}'
    return out[-300:], ''


def _sha_fetch(host: str, rel: str, timeout: float) -> tuple:
    """Скачать файл с выкладки (gzip-aware) → (sha256|'', http-код)."""
    try:
        r = httpx.get(f'https://{host}/{rel}', timeout=timeout,
                      follow_redirects=True, headers={'Accept-Encoding':
                                                      'gzip, deflate'},
                      proxy=None)
        if r.status_code != 200:
            return '', r.status_code
        return hashlib.sha256(r.content).hexdigest(), r.status_code
    except httpx.HTTPError:
        return '', 0


if __name__ == '__main__':
    import argparse

    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('directory')
    _P.add_argument('host')
    _C = _P.parse_args()
    _res = deploy_sha_push(_C.directory, _C.host)
    for _f in _res['files']:
        print(f"{'=' if _f['match'] else 'X'} {_f['file']:<28} "
              f"{_f['local'][:12]}… served={_f['served'][:12] or '—'}… "
              f"[{_f['status']}]")
    print('ERR' if _res['error'] else 'ok', _res['error'],
          _res['pushed'].strip().splitlines()[-1:] or '')
    raise SystemExit(0 if _res.get('ok') else 1)

"""Пачка, не долюбившая лимит: прогнать всё и не словить бан на середине.

Отдать безлимитному с виду API сотню элементов — каждый раз одна история:
на тридцатом 429, процесс гибает или проходит молча половину, остаток
восстанавливается по логам. Здесь — бегунок: вызывает функцию по каждому
элементу, узнаёт rate-limit и в ответе, и в исключении, пережидает
Retry-After или экспоненциально, а прогресс пишет в checkpoint — и после
любого обрыва продолжает с того же элемента, а не с начала. Чем именно
платить элементами (fetch, браузер, пост) — решает вызывающий.

## Что здесь есть

* `function_pace_run` — пачка: пауза + джиттер между элементами, пережидание
  429 по тому же элементу, checkpoint (json со счётчиком) и возобновление;
* `function_pace_rate` — распознать rate-limit: dict с кодом 429/`RATE_LIMITED`
  или исключение со статусом/текстом; вернёт (да/нет, retry_after).

⚠ Бегунок с retry — не алиби: 429 часто значит «ты слишком наглый», а не
«подожди секунд»; переусердствовать с `every` вниз — вежливый способ получить
бан аккаунта.
⚠ `fn` обязана быть идемпотентна или стоик к повтору: retry шлёт тот же
элемент ещё раз, а до этого мог дойти только хвост запроса (создал — 429 —
создал снова).
"""
import json
import os
import random
import time

FUNCTION_PACE_MARKS = ('429', 'rate limit', 'rate_limit', 'ratelimited',
                       'too many', 'лимит')


def function_pace_rate(result=None, exc=None) -> tuple:
    """Rate-limit в ответе или исключении: да/нет и сколько ждать.

    Args:
        result: что вернула функция (dict с кодом/ошибкой — обычная форма
            инструментов py_modules);
        exc: исключение, если оно случилось вместо ответа.

    Returns:
        (bool, retry_after|None) — секунды ожидания, если API назвал их сам.
    """
    def hit(text):
        low = str(text).lower()
        return any(m in low for m in FUNCTION_PACE_MARKS)

    if exc is not None:
        status = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
        headers = getattr(exc, 'response', None)
        after = None
        if headers is not None:
            after = (getattr(headers, 'headers', {}) or {}).get('Retry-After')
        return (str(status) == '429' or hit(exc)), (float(after) if after else None)
    if isinstance(result, dict):
        for key in ('code', 'status', 'status_code'):
            if str(result.get(key, '')) == '429':
                return True, result.get('retry_after')
        err = f"{result.get('error', '')} {result.get('code', '')} {result.get('message', '')}"
        if (result.get('ok') is False or result.get('success') is False) and hit(err):
            return True, result.get('retry_after')
    return False, None


def function_pace_run(items, fn, *, every: float = 2.0, jitter: float = 0.4,
                      retry_base: float = 60.0, retry_cap: float = 1800.0,
                      retries: int = 8, checkpoint: str = '',
                      rate_when=None) -> dict:
    """Прогнать fn по элементам, пережидая rate-limit и сохраняя прогресс.

    Args:
        items: элементы (строки, dict'ы — что понимает fn).
        fn: вызываемая `fn(item)`; ответ и исключение классифицируются
            `rate_when` (по умолчанию `function_pace_rate`).
        every: секунд между элементами (не вниз ради скорости!).
        jitter: плюс-минус случайная добавка к паузе и к пережиданиям.
        retry_base: первая пауза после rate-limit, секунд.
        retry_cap: потолка экспоненты.
        retries: попыток одного элемента, после — элемент помечен сорванным.
        checkpoint: путь к json {'done': N}; есть — продолжение с N, нет —
            с нуля; пишется после каждого элемента.
        rate_when: своя классификация `(result, exc) -> (bool, wait)`.

    Returns:
        {'ok', 'done': сколько всего считается сделанным (с учётом resume),
         'resumed': с какого индекса начали,
         'rows': [{'index', 'item', 'tries', 'result'|'error'}] — только за
         этот запуск.
    """
    rate_when = rate_when or function_pace_rate
    start = 0
    if checkpoint and os.path.exists(checkpoint):
        try:
            start = int(json.load(open(checkpoint)).get('done', 0))
        except (ValueError, OSError):
            start = 0
    rows = []
    done = start

    def save(n):
        if checkpoint:
            try:
                json.dump({'done': n}, open(checkpoint, 'w'))
            except OSError:
                pass

    for i in range(start, len(items)):
        tries, result = 0, None
        while True:
            tries += 1
            exc = None
            try:
                result = fn(items[i])
            except Exception as raised:  # контракт: rate-limit узнан, прочее — в строку
                exc = raised
            limited, wait = rate_when(result, exc)
            if not limited:
                break
            if tries > retries:
                result = {'ok': False, 'error': f'rate-limit после {retries} пережиданий'}
                break
            pause = min(retry_base * 2 ** (tries - 1), retry_cap)
            if wait:
                pause = max(float(wait), 5.0)
            time.sleep(pause + random.uniform(0, jitter))
        row = {'index': i, 'item': items[i], 'tries': tries}
        if isinstance(result, dict) and result.get('ok') is False and 'error' in result:
            row['error'] = result['error'] if len(result) == 1 else result
        else:
            row['result'] = result
        rows.append(row)
        done = i + 1
        save(done)
        if i < len(items) - 1:
            time.sleep(every + random.uniform(0, jitter))
    return {'ok': all('result' in r for r in rows), 'done': done,
            'resumed': start, 'rows': rows}


if __name__ == '__main__':
    import argparse
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0] + ' — демо на fetch')
    _P.add_argument('urls', nargs='*', help='URL’ы; каждый грузится гетом с пережиданием 429')
    _P.add_argument('--every', type=float, default=2.0)
    _P.add_argument('--checkpoint', default='', help='json со счётчиком прогресса')
    _a = _P.parse_args()
    import httpx

    def _fetch(u):
        r = httpx.get(u, follow_redirects=True, timeout=15)
        return {'ok': r.status_code < 400, 'code': r.status_code,
                'retry_after': (r.headers.get('Retry-After') or None)}

    got = function_pace_run(_a.urls, _fetch, every=_a.every, checkpoint=_a.checkpoint)
    for row in got['rows']:
        print(f"{row['index']}\t{row['item']}\ttries={row['tries']}"
              f"\t{row.get('result', row.get('error'))}")

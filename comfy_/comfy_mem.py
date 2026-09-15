"""
Сколько памяти просит граф: короткий прогон с опросом пика.

`_mem` в заголовке workflow — гейт, который решает, отправлять задачу на ферму
или отказать. Ставить его на глаз нельзя: занижен — задача валит ферму вместе с
чужими процессами (единая память GB10), завышен — граф не отправляется никогда.
А узнать его неоткуда: ComfyUI не отчитывается о пике, история прогона его не
помнит.

Модуль закрывает дыру единственным честным способом: гоняет граф и опрашивает
`/system_stats` в это время. Пик = сколько было свободно до минус сколько
осталось в худший момент.

Обучение меряют КОРОТКИМ прогоном: активации и латенты датасета выделяются на
первых же шагах, и восемь шагов показывают тот же пик, что четыре тысячи, —
только за минуты вместо часов. Поэтому у графа обучения число шагов обязано
быть маркером `__STEPS__`.

⚠ Замер идёт снизу вверх. Прогон, который не влез, не «вернёт ошибку»: он
роняет процесс фермы. Поэтому лесенку начинают с заведомо малого и
останавливаются, не доходя до предела, — предсказанный пик выше
`COMFY_MEM_STOP` от общей памяти означает, что следующую ступень не гоняют.
"""
import argparse
import json
import threading
import time

import httpx

# Как часто спрашивать ферму о свободной памяти, с. Чаще — пик точнее, но
# system_stats на занятой ферме отвечает не мгновенно.
COMFY_MEM_POLL = 2.0
# Доля общей памяти, выше которой следующую ступень лесенки не гоняют.
COMFY_MEM_STOP = 0.9


def comfy_mem_probe(run, base, poll=COMFY_MEM_POLL):
    """Выполнить `run()` и вернуть пик памяти фермы; словарь замера.

    `run` — вызываемое без аргументов: отправка графа (`comfy_gen_batch`,
    `comfy_gen_train` — что угодно, что блокируется до конца прогона). Пока оно
    работает, фоновый поток опрашивает `/system_stats`.

    Возвращает `{'total','free_before','free_min','peak','seconds','polls'}`,
    всё в ГиБ: `peak` — сколько граф занял сверх того, что было занято до него.
    Это и есть кандидат в `_mem` (плюс запас `COMFY_GEN_MEM_MARGIN`).
    """
    total, free0 = _mem_stats(base)
    seen = [free0]
    stop = threading.Event()

    def watch():
        while not stop.wait(poll):
            try:
                seen.append(_mem_stats(base)[1])
            except (httpx.HTTPError, KeyError, ValueError):
                pass          # ферма занята или перезапускается — не наше дело

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    started = time.monotonic()
    try:
        result = run()
    finally:
        stop.set()
        t.join(timeout=poll * 2)
    low = min(seen)
    return {'total': round(total, 1), 'free_before': round(free0, 1),
            'free_min': round(low, 1), 'peak': round(free0 - low, 1),
            'seconds': round(time.monotonic() - started, 1),
            'polls': len(seen), 'result': result}


def comfy_mem_fits(peak, total, stop=COMFY_MEM_STOP):
    """Пускать ли следующую ступень лесенки: вердикт и запас."""
    return {'fits': peak < total * stop, 'headroom': round(total * stop - peak, 1),
            'limit': round(total * stop, 1)}


def _mem_stats(base):
    """(всего, свободно) памяти фермы в ГиБ."""
    s = httpx.get(f'{base}/system_stats', timeout=15).json()['system']
    return s['ram_total'] / 2 ** 30, s['ram_free'] / 2 ** 30


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        prog='comfy_mem', description='пик памяти фермы под граф: замер вместо догадки')
    ap.add_argument('command', choices=['stats', 'train'],
                    help='stats — что сейчас; train — короткий прогон обучения с замером')
    ap.add_argument('--base', default='http://127.0.0.1:8188')
    ap.add_argument('--workflow', default='', help='train: API-JSON с маркером __STEPS__')
    ap.add_argument('--files', nargs='*', default=[], help='train: кадры датасета')
    ap.add_argument('--persona', default='', help='train: подпись датасета')
    ap.add_argument('--budget', default='512x512', help='train: бюджет площади кадра')
    ap.add_argument('--steps', type=int, default=8, help='train: шагов в замерном прогоне')
    ap.add_argument('--poll', type=float, default=COMFY_MEM_POLL)
    ns = ap.parse_args()
    try:
        if ns.command == 'stats':
            total, free = _mem_stats(ns.base)
            print(json.dumps({'total': round(total, 1), 'free': round(free, 1)},
                             ensure_ascii=False))
            raise SystemExit
        if not (ns.workflow and ns.files and ns.persona):
            raise SystemExit('train требует --workflow, --files и --persona')
        from comfy_.comfy_gen import comfy_gen_train
        w, h = (int(v) for v in ns.budget.lower().split('x'))
        got = comfy_mem_probe(
            lambda: comfy_gen_train(ns.workflow, ns.files, base=ns.base,
                                    caption=ns.persona, budget=(w, h),
                                    steps=ns.steps),
            ns.base, poll=ns.poll)
        got.pop('result', None)
        got.update(comfy_mem_fits(got['peak'], got['total']))
        got.update({'кадров': len(ns.files), 'бюджет': ns.budget, 'шагов': ns.steps})
        print(json.dumps(got, ensure_ascii=False))
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

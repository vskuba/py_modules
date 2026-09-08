"""
Серия кадров экрана через равные интервалы — сырьё для замеров скорости.

Одиночный `adb_capture` отвечает «что на экране сейчас», а на вопросах вида
«с какой скоростью бежит строка» он буксует: нужны минимум два кадра с
известным разрывом, а ручная петля из `screencap` забывает три вещи, которые
делает этот модуль. Первая — честные метки времени: `interval` между
запусками не равен разрыву кадров (`screencap` сам стоит полсекунды-секунду),
поэтому рядом с каждым файлом пишется `t`, снятый по факту получения байтов,
и Δt для скорости берётся из них, а не из «интервал × номер». Вторая —
мёртвые кадры: тёмный экран (сон, оверлей, анимация перехода) выглядит как
«контент стоит»; кадр с яркостью почти нулевой **и** почти нулевой
дисперсией помечается `dark` — тёмная тема приложения дисперсию даёт, сон
экрана нет, и скорость по помеченным кадрам не считают. Третья — короткий
интервал наврёт: `screencap` на Δt меньше ~1 с часто отдаёт один и тот же
буфер, корреляция даст «стоит» там, где строка прошла 30 px; для бегущей
строки интервал берут ≥ 3 с.

Сама съёмка — `_screencap_png` из `adb_`: голый PNG, без нормализации в
JPEG (замерам нужен побайтовый пиксель, а не пережатый).
"""
import argparse
import os
import time

import numpy as np
from PIL import Image

from adb_.adb_ import _screencap_png

# Пороги «мёртвого» кадра: средний тон и разброс по каналам. Сон экрана —
# почти чёрный и однотонный; тёмная тема приложения тёмная, но пиксели в ней
# различаются (текст, иконки), дисперсия не даст её похоронить.
ADB_BURST_DARK_LUMA = 20.0
ADB_BURST_DARK_STD = 2.0


def adb_burst_capture(n=10, interval=3.0, outdir='/tmp/adb_burst',
                      serial='') -> dict:
    """
    Снять `n` кадров экрана с интервалом; вернуть список кадров с метками.

    пауза между съёмками — ровно `interval` секунд, но сам кадр стоит
    до секунды — фактический разрыв берут из `t`, не из `interval`.

    Args:
        n: сколько кадров (≥ 2, иначе мерить нечего).
        interval: пауза между съёмками, секунды.
        outdir: каталог под кадры (создаётся).
        serial: устройство; пусто — единственное подключённое.

    Returns:
        {'dir', 'interval', 'frames': [{'file', 't', 'luma', 'std',
        'dark'}], 'dark': список индексов тёмных кадров}. `t` — unix-time
        получения байтов; Δt между кадрами — `frames[i+1]['t'] -
        frames[i]['t']`.

    Raises:
        RuntimeError: adb/устройство подвело (сон экрана чаще всего виден
            здесь: `screencap` на экране блокировки ошибётся, а не отдаст
            чёрный кадр).
        ValueError: n < 2.
    """
    if n < 2:
        raise ValueError(f'n={n}: для серии нужно минимум два кадра')
    os.makedirs(outdir, exist_ok=True)
    frames = []
    for i in range(n):
        png = _screencap_png(serial)
        t = time.time()
        path = os.path.join(outdir, f'frame_{i:03d}.png')
        with open(path, 'wb') as fh:
            fh.write(png)
        a = np.asarray(Image.open(path).convert('RGB'), dtype=np.float64)
        luma, std = float(a.mean()), float(a.std())
        dark = luma < ADB_BURST_DARK_LUMA and std < ADB_BURST_DARK_STD
        frames.append({'file': path, 't': round(t, 3), 'luma': round(luma, 1),
                       'std': round(std, 1), 'dark': dark})
        if i < n - 1:
            time.sleep(interval)
    return {'dir': outdir, 'interval': interval, 'frames': frames,
            'dark': [i for i, f in enumerate(frames) if f['dark']]}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Серия снимков экрана через равные интервалы '
                    '(для замеров скорости; Δt — из меток t, не из interval).')
    ap.add_argument('--n', type=int, default=10, help='кадров (≥ 2)')
    ap.add_argument('--interval', type=float, default=3.0,
                    help='пауза между съёмками, с (для бегущей строки ≥ 3)')
    ap.add_argument('--outdir', default='/tmp/adb_burst', help='каталог кадров')
    ap.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    ns = ap.parse_args()
    try:
        r = adb_burst_capture(n=ns.n, interval=ns.interval, outdir=ns.outdir,
                              serial=ns.serial)
        span = r['frames'][-1]['t'] - r['frames'][0]['t']
        print(f"{r['dir']}: кадров {len(r['frames'])}, разброс {span:.1f} с, "
              f"тёмных {len(r['dark'])}"
              + (f" (индексы {r['dark']})" if r['dark'] else ''))
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

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

Второй сценарий — запуск приложения (`adb_burst_launch`): серия с действием
посреди, когда `screenrecord` недоступен. На HyperOS после переподключения USB
он пишет ~0.3 с и молчит без диалога разрешений (мобильная запись ограничена
политикой, а не правами adb) — burst из `screencap` по ~0.3 с на кадр ловит
окна запуска не хуже, а `adb_burst_timeline` превращает серию в таблицу
«что на экране по кадрам»: bbox тёмного контента, цвета в контрольных точках,
маркер смены кадра. Это ответ на вопросы вида «заставка держалась 2.4 с или
1.1» без ffmpeg вообще; системную границу проверяют строкой `Displayed`
из журнала (`adb_log_read`).
"""
import argparse
import glob
import json
import os
import time

import numpy as np
from PIL import Image

from adb_.adb_ import _screencap_png, adb_run

# Пороги «мёртвого» кадра: средний тон и разброс по каналам. Сон экрана —
# почти чёрный и однотонный; тёмная тема приложения тёмная, но пиксели в ней
# различаются (текст, иконки), дисперсия не даст её похоронить.
ADB_BURST_DARK_LUMA = 20.0
ADB_BURST_DARK_STD = 2.0


def adb_burst_capture(n=10, interval=3.0, outdir='/tmp/adb_burst',
                      serial='', during='') -> dict:
    """
    Снять `n` кадров экрана с интервалом; вернуть список кадров с метками.

    пауза между съёмками — ровно `interval` секунд, но сам кадр стоит
    до секунды — фактический разрыв берут из `t`, не из `interval`.

    Args:
        n: сколько кадров (≥ 2, иначе мерить нечего).
        interval: пауза между съёмками, секунды.
        outdir: каталог под кадры (создаётся).
        serial: устройство; пусто — единственное подключённое.
        during: одна shell-строка на устройстве, запущенная один раз сразу
            после первого кадра — первый кадр остаётся «до действия». Строка
            должна возвращаться быстро (`am start …`, `input tap …`); долгая
            команда сдвинет ритм серии, но метки `t` от этого честнее не
            перестают быть — Δt всё равно берут из них.

    Returns:
        {'dir', 'interval', 'frames': [{'file', 't', 'luma', 'std',
        'dark'}], 'dark': список индексов тёмных кадров}. `t` — unix-time
        получения байтов; Δt между кадрами — `frames[i+1]['t'] -
        frames[i]['t']`. Тот же словарь пишется `manifest.json` в outdir —
        по нему `adb_burst_timeline` и CLI `timeline` находят кадры и метки
        после завершения съёмки.

    Raises:
        RuntimeError: adb/устройство подвело (сон экрана чаще всего виден
            здесь: `screencap` на экране блокировки ошибётся, а не отдаст
            чёрный кадр) или сорвалась команда `during`.
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
        if i == 0 and during:
            # действие — после «до»-кадра; синхронно: свои ошибки оно должно
            # поднять отсюда, а не потерять в фоне.
            adb_run('shell', during, serial=serial, timeout=30)
        if i < n - 1:
            time.sleep(interval)
    result = {'dir': outdir, 'interval': interval, 'frames': frames,
              'dark': [i for i, f in enumerate(frames) if f['dark']]}
    if during:
        result['during'] = during
    with open(os.path.join(outdir, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(result, fh, ensure_ascii=False)
    return result


def adb_burst_launch(package: str, n: int = 10, interval: float = 0.3,
                     activity: str = '', outdir: str = '/tmp/adb_burst_launch',
                     serial='') -> dict:
    """
    Холодный запуск приложения серией кадров: стоп → кадр «до» → старт →
    кадры запуска каждые ~`interval`.

    Чем мерить заставку и первые экраны, когда `screenrecord` недоступен или
    ограничен (HyperOS после переподключения USB пишет ~0.3 с). Частота
    ~0.3 с/кадр — потолок `exec-out screencap`, её хватает ловить окна по
    0.3–0.5 с; абсолютные тайминги сверяют со строкой `Displayed` журнала.

    Args:
        package: имя пакета, например `com.example.dia`.
        n: сколько кадров (первый — «до старта»).
        interval: пауза между кадрами; с учётом цены `screencap` кадр ложится
            каждые ~0.3–0.45 с.
        activity: компонент `.Main`/`com.x.MainActivity` — шлётся `am start
            -n package/activity`; пусто — launcher-интент через `monkey`.
        outdir: каталог кадров и `manifest.json`.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        как `adb_burst_capture`, плюс 'package' и 'start' (команда запуска).

    Raises:
        RuntimeError: приложение не поднялось / adb подвело.
    """
    from adb_.adb_app import adb_app_stop  # цикл импортов не плодит: adb_app не знает про burst
    adb_app_stop(package, serial=serial)
    start = (f'am start -n {package}/{activity}' if activity else
             f'monkey -p {package} -c android.intent.category.LAUNCHER 1')
    result = adb_burst_capture(n=n, interval=interval, outdir=outdir,
                               serial=serial, during=start)
    result['package'] = package
    result['start'] = start
    return result


def adb_burst_timeline(frames, band=None, probes=(), luma_max: float = 60.0,
                       min_px: int = 3000, shift_px: int = 10) -> list:
    """
    Разложить серию кадров в таблицу метрик: где тёмный объект и какого он
    цвета — по кадрам, с нечестивой сменой, помеченной `shift`.

    Сырьё — вопрос «сколько держался плашка/заставка и чем она сменилась»:
    глазами по 20 PNG ответа не собрать, а цифры сходятся в одну строку.
    Тёмный контент ищется в полосе `band`, потому что на весь экран тёмных
    пикселей хватает (статус-бар, текст) и bbox поехал бы от них. Кадр без
    объекта (`box=None`) — соседняя страница, гаснущий экран, что угодно:
    отличает его цвет из `probes`, потому и возвращаются оба.

    Args:
        frames: кадры — список dict'ов из `adb_burst_capture`/`launch` или
            просто путей PNG (тогда `t` нет, и сдвиги дают без времени).
        band: (y0, y1, x0, x1) полоса поиска тёмного контента, пиксели;
            пусто — весь кадр.
        probes: контрольные точки ((y, x), ...) — цвет кадра в них, RGB.
        luma_max: какой пиксель считать тёмным (средний по каналам).
        min_px: меньше столько тёмных пикселей — объекта нет (`box=None`).
        shift_px: прыжок bbox больше этого по любой из осей/размеров — смена
            кадра, строка получает shift=True.

    Returns:
        список строк {'file', 't', 'box': {'w','h','cx','cy'}|None,
        'colors': [(r,g,b), ...], 'shift': bool} по порядку кадров.
    """
    rows = []
    prev_box = None
    for one in frames:
        path = one['file'] if isinstance(one, dict) else one
        t = one.get('t') if isinstance(one, dict) else None
        a = np.asarray(Image.open(path).convert('RGB'), dtype=np.float64)
        lum = a.mean(axis=2)
        y0, y1, x0, x1 = band if band else (0, lum.shape[0], 0, lum.shape[1])
        y1, x1 = min(y1, lum.shape[0]), min(x1, lum.shape[1])
        ys, xs = np.nonzero(lum[y0:y1, x0:x1] < luma_max)
        box = None
        if ys.size >= min_px:
            box = {'w': int(xs.max() - xs.min() + 1), 'h': int(ys.max() - ys.min() + 1),
                   'cy': int((ys.max() + ys.min()) / 2) + y0,
                   'cx': int((xs.max() + xs.min()) / 2) + x0}
        colors = [tuple(int(c) for c in a[y, x][:3]) for (y, x) in probes]
        shift = (box is None) != (prev_box is None) or bool(
            box and prev_box and any(abs(box[k] - prev_box[k]) > shift_px
                                     for k in ('w', 'h', 'cx', 'cy')))
        rows.append({'file': os.path.basename(path), 't': t, 'box': box,
                     'colors': colors, 'shift': shift})
        prev_box = box
    return rows


def adb_burst_timeline_text(rows) -> str:
    """
    Таблица `adb_burst_timeline` текстом: `+t` от первого кадра, bbox, цвета
    зондов, `SHIFT` на строках смены кадра. Для CLI и для ответа пользователю
    цифрами без пересказа каждого PNG.
    """
    t0 = next((r['t'] for r in rows if r['t'] is not None), None)
    lines = []
    for r in rows:
        when = f'{r["t"] - t0:+6.2f}s' if r['t'] is not None and t0 is not None else '   ?   '
        box = (f'{r["box"]["w"]}x{r["box"]["h"]}@({r["box"]["cx"]},{r["box"]["cy"]})'
               if r['box'] else '—')
        colors = ' '.join(f'({r0},{g},{b})' for r0, g, b in r['colors'])
        lines.append(f'{when}  {r["file"]:<16} {box:<20} {colors}'
                     + ('  SHIFT' if r['shift'] else ''))
    return '\n'.join(lines)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Серия снимков экрана: cadence-серия для скорости, launch — '
                    'запуск приложения серией, timeline — таблица метрик по кадрам.')
    ap.add_argument('command', nargs='?', default='capture',
                    choices=['capture', 'launch', 'timeline'],
                    help='capture (по умолчанию) | launch ПАКЕТ | timeline [КАТАЛОГ]')
    ap.add_argument('target', nargs='?', default='',
                    help='launch: имя пакета; timeline: каталог серии (по умолчанию --outdir)')
    ap.add_argument('--n', type=int, default=10, help='кадров (≥ 2)')
    ap.add_argument('--interval', type=float, default=3.0,
                    help='пауза между съёмками, с (для бегущей строки ≥ 3; launch — 0.3)')
    ap.add_argument('--outdir', default='/tmp/adb_burst', help='каталог кадров')
    ap.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    ap.add_argument('--during', default='',
                    help='capture: shell-строка на устройстве после первого кадра')
    ap.add_argument('--activity', default='', help='launch: компонент вместо launcher-интента')
    ap.add_argument('--band', type=int, nargs=4, metavar=('Y0', 'Y1', 'X0', 'X1'),
                    default=None, help='timeline: полоса поиска тёмного контента')
    ap.add_argument('--probe', action='append', default=[], metavar='Y,X',
                    help='timeline: контрольная точка цвета (можно несколько)')
    ns = ap.parse_args()
    try:
        if ns.command == 'capture':
            r = adb_burst_capture(n=ns.n, interval=ns.interval, outdir=ns.outdir,
                                  serial=ns.serial, during=ns.during)
        elif ns.command == 'launch':
            if not ns.target:
                raise ValueError('launch требует имя пакета')
            r = adb_burst_launch(ns.target, n=ns.n,
                                 interval=0.3 if ns.interval == 3.0 else ns.interval,
                                 activity=ns.activity,
                                 outdir=ns.outdir if ns.outdir != '/tmp/adb_burst'
                                 else '/tmp/adb_burst_launch',
                                 serial=ns.serial)
        else:
            d = ns.target or ns.outdir
            manifest = os.path.join(d, 'manifest.json')
            if os.path.exists(manifest):
                with open(manifest, encoding='utf-8') as fh:
                    frames = json.load(fh)['frames']
            else:  # серия без манифеста: кадры по имени, времена неизвестны
                frames = sorted(glob.glob(os.path.join(d, '*.png')))
            probes = [tuple(int(v) for v in p.split(',')) for p in ns.probe]
            print(adb_burst_timeline_text(
                adb_burst_timeline(frames, band=ns.band, probes=probes)))
            raise SystemExit(0)
        span = r['frames'][-1]['t'] - r['frames'][0]['t']
        print(f"{r['dir']}: кадров {len(r['frames'])}, разброс {span:.1f} с, "
              f"тёмных {len(r['dark'])}"
              + (f" (индексы {r['dark']})" if r['dark'] else ''))
    except (RuntimeError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

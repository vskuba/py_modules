"""
Экран телефона видеозаписью: `screenrecord` с действием посреди записи и разбор
готового mp4 на кадры.

Скриншот ловит один миг, а переходы между страницами (тап по вкладке, всплытие
клавиатуры, появление попапа) живут в движении: чтобы увидеть, что мелькает
между кадрами, нужен mp4 и его кадры.

Две грабли, обе наступают молча:

- mp4 `screenrecord` дописывается только по `SIGINT` или по исчерпании
  `--time-limit`; стянуть файл раньше — получить битый контейнер, который
  ffmpeg не откроет. Поэтому потолок времени задаётся всегда, а `adb pull`
  идёт только после того, как процесс adb завершился сам;
- запись стартует не мгновенно: тап, отданный в ту же секунду, попадает в
  тишину до первой записанной рамки. Отсюда выдержка `ADB_REC_START_DELAY`.

`adb_rec_frames` разбирает готовый mp4 `ffmpeg`-ом — системной зависимостью
(как poppler для генератора PDF): колесом в pip его не поставить, отсутствие —
отдельная ошибка, а не тихий отказ.
"""
import argparse
import os
import shutil
import subprocess
import time

from adb_.adb_ import adb_run
from adb_.adb_file import adb_file_pull

# Потолок `--time-limit` у screenrecord, секунды; больше устройство не пишет.
ADB_REC_LIMIT = 180

# Пауза перед действием, секунды: screenrecord начинает писать не сразу,
# действие раньше первой рамки — действие вне кадра.
ADB_REC_START_DELAY = 1.0

# Сколько секунд сверх потолка ждём завершения записи; свой ход — ошибка.
ADB_REC_TAIL = 10.0


def adb_rec_record(out: str = '/tmp/adb_rec.mp4', during: str = '',
                   seconds: int = 10, serial: str = '') -> dict:
    """
    Записать экран устройства mp4 и стянуть его на машину.

    Args:
        out: куда положить mp4.
        during: одна shell-строка на устройстве, выполненная посреди записи
            (после выдержки `ADB_REC_START_DELAY`), например `input tap 540 2200`
            или `am start -n ua.gov.diia.app/.MainActivity`.
        seconds: длительность записи, от 1 до `ADB_REC_LIMIT`.
        serial: устройство; пусто — единственное подключённое.

    Returns:
        {'file', 'bytes', 'seconds'}.

    Raises:
        RuntimeError: adb/устройство подвело, запись не завершилась или
            файл не пришёл; запись и временный файл на устройстве погасятся/
            удалятся в любом случае.
    """
    seconds = max(1, min(int(seconds), ADB_REC_LIMIT))
    remote = f'/sdcard/adb_rec-{int(time.time())}.mp4'
    proc = subprocess.Popen(
        ['adb'] + (['-s', serial] if serial else []) +
        ['shell', 'screenrecord', '--time-limit', str(seconds), remote],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(ADB_REC_START_DELAY)
        if during:
            adb_run('shell', during, serial=serial, timeout=seconds + 5)
        try:
            proc.wait(timeout=seconds + ADB_REC_TAIL)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'экран не перестал писаться за {seconds + ADB_REC_TAIL:g} с')
        pulled = adb_file_pull(remote, os.path.dirname(os.path.abspath(out)) or '.', serial=serial)
        os.replace(pulled, out)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        adb_run('shell', 'rm', '-f', remote, serial=serial, timeout=10)
    return {'file': out, 'bytes': os.path.getsize(out), 'seconds': seconds}


def adb_rec_frames(mp4: str, out_dir: str = '/tmp/adb_rec_frames',
                   times=(), fps: float = 0) -> list:
    """
    Разобрать mp4 на PNG-кадры; вернуть список путей.

    Кадр на указанное время ищется точным поиском (`-ss` после `-i`): у
    screenrecord ключевые кадры ~раз в секунду, быстрый поиск перед `-i`
    притянул бы кадр к ближайшему ключевому и «момент тапа» приехал бы соседним.

    Args:
        mp4: файл записи (свежесть проверяет сам ffmpeg, битый контейнер — его
            ненулевой выход).
        out_dir: каталог для кадров, создаётся.
        times: моменты в секундах (числа или строки «0.5»), кадр на каждый;
            имена `f0.50.png` — время в имени, чтобы не переспаривать с событиями.
        fps: если times пусто — брать каждые 1/fps секунды, имена `f-%04d.png`.

    Raises:
        FileNotFoundError: ffmpeg не установлен или mp4 нет.
        ValueError: не задано ни times, ни fps.
    """
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise FileNotFoundError('ffmpeg не найден в PATH — это системный пакет, '
                                'кадров из mp4 без него не будет')
    if not os.path.isfile(mp4):
        raise FileNotFoundError(f'запись не найдена: {mp4}')
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    if times:
        for t in times:
            t = float(t)
            path = os.path.join(out_dir, f'f{t:.2f}.png')
            _ffmpeg(ffmpeg, ['-ss', f'{t:.3f}', '-frames:v', '1', path], mp4)
            paths.append(path)
    elif fps:
        pattern = os.path.join(out_dir, 'f-%04d.png')
        _ffmpeg(ffmpeg, ['-vf', f'fps={float(fps):g}', pattern], mp4)
        paths = sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir)
                       if f.startswith('f-'))
    else:
        raise ValueError('нужны times (моменты) или fps (частота)')
    return paths


def _ffmpeg(ffmpeg: str, after_i: list, mp4: str) -> None:
    """ffmpeg на один выход; точный `-ss` стоит после `-i`, поэтому он в after_i."""
    cmd = [ffmpeg, '-loglevel', 'error', '-i', mp4] + after_i
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f'ffmpeg {mp4}: {done.stderr.strip()[-200:]}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Видеоэкран устройства: запись с действием посреди и '
                    'разбор mp4 на кадры.')
    ap.add_argument('command', choices=['record', 'frames'])
    ap.add_argument('mp4', nargs='?', default='/tmp/adb_rec.mp4',
                    help='файл записи (frames) или куда писать (record)')
    ap.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    ap.add_argument('--seconds', type=int, default=10, help='длительность записи, до 180')
    ap.add_argument('--during', default='', help='shell-строка на устройстве посреди записи')
    ap.add_argument('--out-dir', default='/tmp/adb_rec_frames', help='каталог кадров')
    ap.add_argument('--times', default='', help='моменты через запятую: 0.5,1.25')
    ap.add_argument('--fps', type=float, default=0, help='частота кадров, если times пусто')
    ns = ap.parse_args()
    try:
        if ns.command == 'record':
            res = adb_rec_record(out=ns.mp4, during=ns.during,
                                 seconds=ns.seconds, serial=ns.serial)
            print(f"{res['file']} ({res['bytes']} байт, {res['seconds']} с)")
        else:
            for path in adb_rec_frames(ns.mp4, out_dir=ns.out_dir,
                                       times=[t for t in ns.times.split(',') if t],
                                       fps=ns.fps):
                print(path)
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

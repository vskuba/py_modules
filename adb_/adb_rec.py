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
отдельная ошибка, а не тихий отказ. Тем же ffmpeg-ом делаются обзорная сетка
`adb_rec_sheet` (вся запись читается одним изображением) и парная сверка
записей `adb_rec_compare` (оригинал против клона на одних и тех же моментах).
"""
import argparse
import os
import shutil
import subprocess
import tempfile
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

# Запись короче этой длины **и** завершённая раньше срока — не запись: так
# HyperOS после переподключения USB молча обрывает screenrecord ~на 0.3 с
# (диалога разрешений на экране нет, права adb ни при чём). Сам по себе
# короткий mp4 ничего не доказывает: статичный экран даёт редкие кадры и
# маленькую длительность метаданных при исправной записи (на эмуляторе
# 2-секундный статичный файл читается как 0.7 с) — поэтому решение принимает
# пара «длительность + сколько реально прожил процесс».
ADB_REC_MIN_KEEP = 1.0

# Обзорная сетка: кадры раз в ADB_REC_SHEET_FPS секунд, колонками по
# ADB_REC_SHEET_COLS, ширина кадра ADB_REC_SHEET_WIDTH px. Ширины хватает
# прочесть заголовок экрана, а вся двухминутная запись остаётся на одном листе.
ADB_REC_SHEET_FPS = 2
ADB_REC_SHEET_COLS = 5
ADB_REC_SHEET_WIDTH = 270

# Потолок строк обзорной сетки: выше — изображение в десятки тысяч пикселей,
# такое не обозреть, поэтому длинную запись сетка прореживает по fps сама.
ADB_REC_SHEET_MAX_ROWS = 40


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
        {'file', 'bytes', 'seconds', 'recorded'} — `recorded` фактическая
        длина в секундах по ffprobe (None если ffprobe нет в PATH); короткая
        запись — ошибка, а не тихий укороченный файл.

    Raises:
        RuntimeError: adb/устройство подвело, запись не завершилась, файл
            не пришёл или записалось меньше `ADB_REC_MIN_KEEP` с (ограничение
            устройства); запись и временный файл на устройстве погасятся/
            удалятся в любом случае.
    """
    seconds = max(1, min(int(seconds), ADB_REC_LIMIT))
    remote = f'/sdcard/adb_rec-{int(time.time())}.mp4'
    started = time.monotonic()
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
            raise RuntimeError(
                f'экран не перестал писаться за {seconds + ADB_REC_TAIL:g} с; '
                f'если устройство ограничивает запись (HyperOS после переподключения '
                f'USB), фолбек — серия кадров: python -m adb_.adb_burst launch ПАКЕТ')
        pulled = adb_file_pull(remote, os.path.dirname(os.path.abspath(out)) or '.', serial=serial)
        os.replace(pulled, out)
        lived = time.monotonic() - started
        recorded = None
        try:
            recorded = _ffprobe_duration(out)
        except FileNotFoundError:
            pass  # без ffprobe длина недоступна: саму запись это не отменяет
        # Ранний выход процесса — единственный честный признак обрыва:
        # статичный экран даёт короткую длительность метаданных и при
        # исправной записи (эмулятор из 2 с пишет 0.7), HyperOS при обрыве
        # забирает и процесс, и файл.
        if lived < seconds * 0.5 and (recorded is None or recorded < ADB_REC_MIN_KEEP):
            raise RuntimeError(
                f'screenrecord прожил {lived:.1f} с из {seconds} с и записал '
                f'{recorded:.1f} с: так HyperOS молча обрывает запись после '
                f'переподключения USB (диалога разрешений нет, перетыкать '
                f'бесполезно). Фолбек — серия кадров: python -m adb_.adb_burst '
                f'launch ПАКЕТ, см. mobile_device.md')
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        adb_run('shell', 'rm', '-f', remote, serial=serial, timeout=10)
    return {'file': out, 'bytes': os.path.getsize(out), 'seconds': seconds,
            'recorded': round(recorded, 2) if recorded is not None else None}


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
    ffmpeg = _ffmpeg_bin()
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


def adb_rec_sheet(mp4: str, out: str = '/tmp/adb_rec_sheet.png',
                  fps: float = ADB_REC_SHEET_FPS, cols: int = ADB_REC_SHEET_COLS,
                  width: int = ADB_REC_SHEET_WIDTH) -> str:
    """
    Обзорная сетка кадров: вся запись читается одним изображением.

    Десяток одиночных кадров, открытых по очереди, теряет последовательность;
    сетка показывает таймлайн движения одним взглядом — где начался переход,
    что мелькнуло между состояниями. Кадры отбираются `fps`-фильтром за одно
    декодирование и склеиваются Pillow-ом: у ffmpeg-овского `tile` нет
    сброса неполного ряда, и хвост записи — самый интересный — терялся бы
    молча. Каждая клетка подписана временем на плашке, неполный последний
    ряд доклеивается чёрным.

    Args:
        mp4: файл записи.
        out: куда положить png.
        fps: частота кадров сетки; ряды выше разумного потолка автоматически
            прореживают fps — лист обязан оставаться обозримым.
        cols: колонок в сетке.
        width: ширина кадра в сетке, px.

    Returns:
        Путь записанного png.

    Raises:
        FileNotFoundError: ffmpeg/ffprobe не установлены или mp4 нет.
        RuntimeError: ffmpeg не вытащил ни одного кадра.
    """
    from PIL import Image        # тяжёлый стек — лениво, он нужен только склейке
    ffmpeg = _ffmpeg_bin()
    if not os.path.isfile(mp4):
        raise FileNotFoundError(f'запись не найдена: {mp4}')
    duration = _ffprobe_duration(mp4)
    if duration * fps / cols > ADB_REC_SHEET_MAX_ROWS:
        fps = ADB_REC_SHEET_MAX_ROWS * cols / duration
    os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='adb_rec_sheet-')
    try:
        pattern = os.path.join(tmp, 'f-%04d.png')
        # Длинная запись декодируется не секунду: потолок щедрый, иначе sheet
        # падал бы таймаутом ровно на тех записях, для которых и нужен.
        _ffmpeg(ffmpeg, ['-vf', f'fps={fps:g},scale={int(width)}:-1:flags=lanczos',
                         pattern], mp4, timeout=300)
        frames = sorted(os.path.join(tmp, f) for f in os.listdir(tmp))
        if not frames:
            raise RuntimeError(f'ffmpeg не вытащил кадров из {mp4}')
        cells = [_rec_cell(p, width) for p in frames]
        grid, chips = [], []
        for i in range(0, len(cells), cols):
            chunk = cells[i:i + cols]
            grid.append(chunk + [Image.new('RGB', (cells[0].width, chunk[0].height))
                                 for _ in range(cols - len(chunk))])
            chips.append([f't={j / fps:g}' if j < len(cells) else ''
                          for j in range(i, i + cols)])
        return _grid_png(grid, chips, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def adb_rec_compare(mp4_a: str, mp4_b: str, times,
                    out: str = '/tmp/adb_rec_compare.png',
                    labels=('A', 'B'), cell_width: int = ADB_REC_SHEET_WIDTH) -> str:
    """
    Две записи рядом на одних и тех же моментах: сравнение оригинала и клона.

    Кадры берутся точным поиском (`adb_rec_frames`) из обоих файлов по одним и
    тем же временам и склеиваются парами: строка — момент, колонки — записи,
    каждая клетка подписана меткой и временем. Это финальная сверка «клон
    открывается как оригинал»: сравнивать нужно записи с записями — экран,
    записанный screenrecord, темнее исходных hex-ов, и прямое сравнение
    записи с макетом даёт ложную разницу.

    Args:
        mp4_a, mp4_b: файлы записей.
        times: моменты в секундах, общий таймлайн обеих записей (отсчёт от
            старта записи каждого файла).
        out: куда положить png.
        labels: подписи колонок (Cyrillic выдерживается, если в системе есть
            DejaVu; шрифт подбирается приватно).
        cell_width: ширина клетки, px.

    Returns:
        Путь записанного png.

    Raises:
        FileNotFoundError: ffmpeg не установлен, файлов записей нет.
        ValueError: times пусто.
        RuntimeError: ffmpeg не вытащил кадр.
    """
    times = [float(t) for t in times]
    if not times:
        raise ValueError('нужны times: моменты, общие для обеих записей')
    for path in (mp4_a, mp4_b):
        if not os.path.isfile(path):
            raise FileNotFoundError(f'запись не найдена: {path}')
    tmp = tempfile.mkdtemp(prefix='adb_rec_compare-')
    try:
        frames_a = adb_rec_frames(mp4_a, os.path.join(tmp, 'a'), times=times)
        frames_b = adb_rec_frames(mp4_b, os.path.join(tmp, 'b'), times=times)
        lab = (list(labels) + ['A', 'B'])[:2]
        grid = [[_rec_cell(pa, cell_width), _rec_cell(pb, cell_width)]
                for pa, pb in zip(frames_a, frames_b)]
        chips = [[f'{lab[0]} t={t:g}', f'{lab[1]} t={t:g}'] for t in times]
        # Плашки разных цветов: глаз собирает пары раньше, чем прочитается текст.
        out = _grid_png(grid, chips, out,
                        chip_colors=((176, 42, 42), (38, 110, 52)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def _ffmpeg(ffmpeg: str, after_i: list, mp4: str, timeout: int = 60) -> None:
    """ffmpeg на один выход; точный `-ss` стоит после `-i`, поэтому он в after_i."""
    cmd = [ffmpeg, '-loglevel', 'error', '-i', mp4] + after_i
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f'ffmpeg {mp4}: {done.stderr.strip()[-200:]}')


def _ffmpeg_bin() -> str:
    """ffmpeg из PATH — системная зависимость, отсутствие которой названо прямо."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise FileNotFoundError('ffmpeg не найден в PATH — это системный пакет, '
                                'кадров из mp4 без него не будет')
    return ffmpeg


def _ffprobe_duration(mp4: str) -> float:
    """
    Длительность записи, секунды; ffprobe едет в комплекте с ffmpeg.

    Берёт первое число из format=duration, а если контейнер его не несёт
    (эмулятор пишет moov без общей длительности — ffprobe отвечает `N/A` при
    исправном файле) — из длительностей потоков.
    """
    probe = shutil.which('ffprobe')
    if not probe:
        raise FileNotFoundError('ffprobe не найден в PATH — он идёт с ffmpeg, '
                                'без него сетку не размечать по рядам')
    done = subprocess.run([probe, '-v', 'error', '-show_entries',
                           'format=duration:stream=duration',
                           '-of', 'default=nk=1:nw=1', mp4],
                          capture_output=True, text=True, timeout=30)
    for word in done.stdout.split():
        try:
            return float(word)
        except ValueError:
            continue
    raise RuntimeError(f'ffprobe не назвал длительность {mp4}') from None


def _rec_cell(path: str, width: int):
    """Клетка сетки сравнения: кадр, ужатый до ширины width с сохранением пропорций."""
    from PIL import Image        # тяжёлый стек — лениво, он нужен только склейке
    im = Image.open(path).convert('RGB')
    h = max(1, round(im.height * width / im.width))
    return im.resize((width, h), Image.LANCZOS)


def _grid_png(grid, chips, out: str,
              chip_colors=((90, 90, 90),)) -> str:
    """
    Склеить строки клеток в лист; над каждой клеткой — плашка chips[row][col].

    Пустая строка плашки не рисуется: клетка остаётся голой. Плашка ложится
    поверх кадра, а не отдельной строкой, — лист остаётся кратным кадрам и по
    высоте, и по ширине.
    """
    from PIL import Image, ImageDraw     # лениво, как выше
    gap = 8
    cw = grid[0][0].width
    canvas = Image.new('RGB', (cw * len(grid[0]) + gap * (len(grid[0]) + 1),
                               sum(max(c.height for c in r) + gap for r in grid) + gap),
                       (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    font = _label_font()
    y = gap
    for row, row_chips in zip(grid, chips):
        for col, (cell, chip) in enumerate(zip(row, row_chips)):
            x = gap + col * (cw + gap)
            canvas.paste(cell, (x, y))
            if chip:
                w = int(draw.textlength(chip, font=font)) + 12
                draw.rectangle((x, y, x + w, y + 26),
                               fill=chip_colors[col % len(chip_colors)])
                draw.text((x + 6, y + 4), chip, fill=(255, 255, 255), font=font)
        y += max(c.height for c in row) + gap
    os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
    canvas.save(out)
    return out


def _label_font(size: int = 15):
    """
    Шрифт подписей с кириллицей: DejaVu с системы, иначе встроенный растровый.

    Встроенный шрифт Pillow (Aileron) кириллицы не имеет и рисует вместо неё
    пустые квадраты — подпись «оригинал» превращается в рябь, поэтому первым
    пробуется DejaVu, который стоит практически везде.
    """
    from PIL import ImageFont
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/usr/share/fonts/dejavu/DejaVuSans.ttf',
                 '/usr/share/fonts/TTF/DejaVuSans.ttf'):
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)      # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Видеоэкран устройства: запись с действием посреди, разбор '
                    'mp4 на кадры, обзорная сетка, парная сверка двух записей.')
    ap.add_argument('command', choices=['record', 'frames', 'sheet', 'compare'])
    ap.add_argument('mp4', nargs='?', default='/tmp/adb_rec.mp4',
                    help='файл записи (куда писать для record)')
    ap.add_argument('mp4_b', nargs='?', default='',
                    help='вторая запись для compare')
    ap.add_argument('--serial', default='', help='устройство; по умолчанию единственное')
    ap.add_argument('--seconds', type=int, default=10, help='длительность записи, до 180')
    ap.add_argument('--during', default='', help='shell-строка на устройстве посреди записи')
    ap.add_argument('--out-dir', default='/tmp/adb_rec_frames', help='каталог кадров')
    ap.add_argument('--out', default='', help='лист для sheet/compare '
                    '(пусто — имя по умолчанию)')
    ap.add_argument('--times', default='', help='моменты через запятую: 0.5,1.25')
    ap.add_argument('--fps', type=float, default=0,
                    help='частота кадров (frames — если times пусто; sheet)')
    ap.add_argument('--cols', type=int, default=ADB_REC_SHEET_COLS, help='колонок в sheet')
    ap.add_argument('--cell-width', type=int, default=ADB_REC_SHEET_WIDTH,
                    help='ширина клетки в sheet/compare, px')
    ap.add_argument('--labels', default='A,B', help='подписи колонок compare')
    ns = ap.parse_args()
    times = [t for t in ns.times.split(',') if t]
    try:
        if ns.command == 'record':
            res = adb_rec_record(out=ns.mp4, during=ns.during,
                                 seconds=ns.seconds, serial=ns.serial)
            print(f"{res['file']} ({res['bytes']} байт, {res['seconds']} с)")
        elif ns.command == 'sheet':
            print(adb_rec_sheet(ns.mp4, out=ns.out or '/tmp/adb_rec_sheet.png',
                                fps=ns.fps or ADB_REC_SHEET_FPS,
                                cols=ns.cols, width=ns.cell_width))
        elif ns.command == 'compare':
            if not ns.mp4_b:
                raise SystemExit('compare требует вторую запись: compare a.mp4 b.mp4')
            labels = tuple(s.strip() for s in ns.labels.split(','))
            print(adb_rec_compare(ns.mp4, ns.mp4_b, times,
                                  out=ns.out or '/tmp/adb_rec_compare.png',
                                  labels=labels, cell_width=ns.cell_width))
        else:
            for path in adb_rec_frames(ns.mp4, out_dir=ns.out_dir,
                                       times=times, fps=ns.fps):
                print(path)
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

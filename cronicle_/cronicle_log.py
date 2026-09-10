"""Уборка логов Cronicle: держим только последние дни, остальное сносим.

Cronicle убирает за собой наполовину. Свой суточный лог он ротирует сам —
складывает в `logs/archives/[yyyy]/[mm]/[dd]/[filename]-…log.gz` (настройка
`log_archive_path` в его `conf/config.json`), — но архивы потом не удаляет
никогда. Живьём так набралось 208 файлов и 30 МБ за два месяца.

Вторая половина, `data/jobs` с историей прогонов, к этому модулю отношения не
имеет: её чистит сам Cronicle в ночное обслуживание (`maintenance`) по настройке
`job_data_expire_days`. Её выставляют переменной окружения
`CRONICLE_job_data_expire_days` — не здесь. Мы только показываем размер этой кучи,
и вот зачем: настройка из переменной применяется **в память**, на диске в
`config.json` остаётся прежнее значение, и проверить, доехала ли она, иначе как по
размеру каталога нечем.

⚠ **Где лежат логи, знает вызывающий.** Путь по умолчанию — `data/cronicle` от
корня проекта: так его монтирует compose, и так это работало до переноса в общий
слой. Проект, который монтирует Cronicle иначе, передаёт свой каталог параметром —
зашитый путь сделал бы модуль непереносимым.
"""

import os
import time

from logging_.logging_ import logger_info
from project_.project_ import project_root

# Каталог Cronicle по умолчанию, от корня проекта. Внутри него — и логи
# (`logs/`), и история прогонов (`data/jobs`).
CRONICLE_LOG_BASE = os.path.join('data', 'cronicle')

# Каталоги, которые чистим, — относительно каталога Cronicle. Оба внутри его
# логов: архивы суточной ротации и логи отдельных прогонов. Второй обычно пуст
# (Cronicle пишет их в `data/jobs`), но встречается на старых инсталляциях, и
# оставлять его расти незачем.
CRONICLE_LOG_DIRS = (
    os.path.join('logs', 'archives'),
    os.path.join('logs', 'jobs'),
)

# Сколько суток логов держим.
CRONICLE_LOG_KEEP_DAYS = 3

# История прогонов: её чистит сам Cronicle, мы только показываем размер (см.
# докстринг модуля).
CRONICLE_LOG_JOBS_DIR = os.path.join('data', 'jobs')

# ⚠ Страховка от чужого каталога живёт в `_inside`: чистим только то, что лежит
# **внутри** переданного каталога Cronicle. Проверяется родство путей, а не
# вхождение подстроки: до переноса здесь стоял маркер `data/cronicle/logs`, и он
# работал ровно до первого проекта, который смонтировал Cronicle иначе — там
# уборка молча не делала бы ничего. Молчаливое бездействие хуже отказа: каталог
# растёт, а кронджоб рапортует об успехе.


def cronicle_log_prune(keep_days: int = CRONICLE_LOG_KEEP_DAYS, base_dir: str = '') -> dict:
    """
    Удалить логи Cronicle старше `keep_days` суток.

    Args:
        keep_days: сколько последних суток оставить. Меньше единицы не бывает:
            ноль означал бы «снести и сегодняшний лог», в том числе тот, в который
            Cronicle пишет прямо сейчас.
        base_dir: каталог Cronicle. Пусто — `data/cronicle` от корня проекта.

    Returns:
        `{'files': int, 'bytes': int, 'dirs': int, 'kept_days': int}` — сколько
        файлов и байт освободили и сколько опустевших каталогов убрали.
    """
    keep_days = max(int(keep_days or 0), 1)
    deadline = time.time() - keep_days * 86400
    stats = {'files': 0, 'bytes': 0, 'dirs': 0, 'kept_days': keep_days}

    base = _base(base_dir)
    for rel_dir in CRONICLE_LOG_DIRS:
        root = os.path.join(base, rel_dir)
        if not os.path.isdir(root) or not _inside(root, base):
            continue
        _prune_dir(root, deadline, stats)

    logger_info(f"🧹 Логи Cronicle: удалено файлов {stats['files']}, "
                f"освобождено {stats['bytes'] // 1024} КБ, каталогов {stats['dirs']}, "
                f"оставлены последние {keep_days} сут.")
    return stats


def cronicle_log_sizes(base_dir: str = '') -> dict:
    """
    Размер обеих куч Cronicle в байтах: архивы логов и история прогонов.

    Returns:
        `{'archives': int, 'jobs': int}`. Каталога нет — ноль, это не ошибка:
        на свежей установке Cronicle ещё ничего не ротировал.
    """
    base = _base(base_dir)

    return {
        'archives': _dir_size(os.path.join(base, CRONICLE_LOG_DIRS[0])),
        'jobs': _dir_size(os.path.join(base, CRONICLE_LOG_JOBS_DIR)),
    }


def cronicle_log_report(stats: dict, sizes: dict) -> str:
    """
    Строка отчёта: что убрали и сколько осталось.

    Отчёт короткий и уходит раз в сутки, поэтому без заголовков и списков —
    человек читает его мельком в ленте уведомлений.
    """
    return (f"🧹 Логи Cronicle (держим {stats.get('kept_days', CRONICLE_LOG_KEEP_DAYS)} сут.): "
            f"убрано {stats.get('files', 0)} файлов, {_size_text(stats.get('bytes', 0))}. "
            f"Осталось: архивы {_size_text(sizes.get('archives', 0))}, "
            f"история прогонов {_size_text(sizes.get('jobs', 0))}.")


def _base(base_dir: str) -> str:
    """Каталог Cronicle: переданный либо `data/cronicle` от корня проекта."""
    if base_dir:
        return os.path.abspath(base_dir)

    return os.path.join(str(project_root()), CRONICLE_LOG_BASE)


def _inside(path: str, base: str) -> bool:
    """
    Лежит ли `path` внутри `base` — страховка перед удалением (см. комментарий выше).

    Оба пути приводятся к абсолютным и разыменовываются: симлинк наружу иначе
    прошёл бы проверку и увёл уборку в чужой каталог. Сам `base` не считается
    «внутри себя» — снести каталог Cronicle целиком уборка не должна.
    """
    try:
        path, base = os.path.realpath(path), os.path.realpath(base)

        return path != base and os.path.commonpath([path, base]) == base
    except (OSError, ValueError):
        # ValueError — пути на разных дисках (Windows) либо смесь относительного с
        # абсолютным. И то, и другое означает «не внутри», а не повод падать.
        return False


def _dir_size(path: str) -> int:
    """
    Место на диске под каталогом — то же, что показывает `du`.

    Сами каталоги тоже считаем, и это не мелочь: Cronicle раскладывает историю
    прогонов по вложенным хеш-каталогам, их там сотни тысяч, и на диске они
    занимают больше, чем сами файлы. Без этого счёта отчёт расходился с `du`
    вдвое. Симлинки считаем по себе, а не по цели.
    """
    if not os.path.isdir(path):
        return 0

    total = 0
    for dir_path, _dir_names, file_names in os.walk(path):
        for name in [None] + list(file_names):
            entry = dir_path if name is None else os.path.join(dir_path, name)
            try:
                total += _file_size(os.lstat(entry))
            except OSError:
                continue

    return total


def _file_size(info) -> int:
    """
    Занятое место, а не длина файла: считаем блоки, как `du`.

    Разница здесь не косметическая. В истории прогонов у Cronicle набирается
    110 тысяч крошечных файлов: по длине это 121 МБ, а на диске — 1.1 ГБ, потому
    что каждый занимает целый блок. Отчёт о длине занижал бы расход диска почти в
    десять раз, а весь смысл отчёта — вовремя увидеть, что каталог растёт.

    `st_blocks` считается в единицах по 512 байт независимо от файловой системы.
    Его нет на Windows — там откатываемся на длину: сервис живёт в linux-
    контейнерах, и эта ветка нужна только чтобы модуль импортировался где угодно.
    """
    blocks = getattr(info, 'st_blocks', None)

    return blocks * 512 if blocks is not None else info.st_size


def _size_text(size: int) -> str:
    """
    Байты человеку: КБ, МБ, ГБ по порогам.

    Ступеней три, потому что отчёт читают мельком: «0.0 МБ» на пустом каталоге и
    «1765.9 МБ» на полном одинаково бесполезны — в первом не видно, что там что-то
    есть, во втором не сосчитать порядок с одного взгляда.
    """
    size = int(size or 0)
    if size < 1024 * 1024:
        return f'{size // 1024} КБ'
    if size < 1024 * 1024 * 1024:
        return f'{size / (1024 * 1024):.1f} МБ'

    return f'{size / (1024 * 1024 * 1024):.2f} ГБ'


def _prune_dir(root: str, deadline: float, stats: dict) -> None:
    """Обойти каталог снизу вверх: сначала файлы, потом опустевшие каталоги."""
    for dir_path, _dir_names, file_names in os.walk(root, topdown=False):
        for name in file_names:
            path = os.path.join(dir_path, name)
            _prune_file(path, deadline, stats)

        # Сам `root` не трогаем: Cronicle пишет в него и заново не создаст.
        if dir_path != root:
            _prune_empty_dir(dir_path, stats)


def _prune_file(path: str, deadline: float, stats: dict) -> None:
    """Удалить файл, если он старше срока. Симлинки не трогаем и не разыменовываем."""
    try:
        info = os.lstat(path)
    except OSError:
        return
    if info.st_mtime >= deadline:
        return

    try:
        os.remove(path)
    except OSError as e:
        # Занятый или чужой файл — не повод ронять уборку: остальное всё равно чистим.
        logger_info(f"🧹 Логи Cronicle: не удалось убрать {path}: {type(e).__name__}")
        return

    stats['files'] += 1
    stats['bytes'] += _file_size(info)


def _prune_empty_dir(path: str, stats: dict) -> None:
    """Убрать каталог, если в нём ничего не осталось: иначе копятся пустые даты."""
    try:
        if os.listdir(path):
            return
        os.rmdir(path)
    except OSError:
        return

    stats['dirs'] += 1

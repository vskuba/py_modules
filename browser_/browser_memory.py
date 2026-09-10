"""Замер памяти контейнера-браузера.

Нужен не для мониторинга, а чтобы отвечать на единственный вопрос, от которого
зависит вся затея: сколько на самом деле стоит одна живая страница. Проектные
оценки (30-80 МБ на страницу, до 200 МБ на тяжёлую SPA) — оценки; предел сессий
ставится по замеру, а не по ним.

Считаем два разных числа, и путать их нельзя:
  * `container_mb` — сколько с контейнера спрашивает ядро (cgroup). Именно это
    число упирается в `mem_limit` и убивает контейнер по OOM.
  * `chromium_pss_mb` — сколько занимают процессы Chromium, с честным делением
    общей памяти между ними. RSS тут врёт вдвое и больше: рендереры Chromium
    делят один и тот же код и данные, и RSS считает их каждому целиком.
"""

import psutil

# cgroup v2 и v1 лежат в разных местах; какой в системе — узнаём попыткой чтения.
BROWSER_MEMORY_CGROUP_V2_USAGE = '/sys/fs/cgroup/memory.current'
BROWSER_MEMORY_CGROUP_V2_LIMIT = '/sys/fs/cgroup/memory.max'
BROWSER_MEMORY_CGROUP_V1_USAGE = '/sys/fs/cgroup/memory/memory.usage_in_bytes'
BROWSER_MEMORY_CGROUP_V1_LIMIT = '/sys/fs/cgroup/memory/memory.limit_in_bytes'

# Имена процессов браузера: у полного Chromium — `chrome`, у headless-shell,
# который обычно ставят в образ, — `headless_shell`.
BROWSER_MEMORY_PROCESS_NAMES = ('chrome', 'headless_shell')


def browser_memory_stat() -> dict:
    """Память контейнера и разбивка по процессам Chromium."""
    usage, limit = _cgroup_memory()
    chromium_pss, chromium_rss, count = _chromium_memory()

    stat = {
        'container_mb': _mb(usage),
        'container_limit_mb': _mb(limit),
        'chromium_pss_mb': _mb(chromium_pss),
        'chromium_rss_mb': _mb(chromium_rss),
        'chromium_processes': count,
    }
    return stat


def _cgroup_memory() -> tuple[int, int]:
    """(занято, предел) в байтах. Предел 0 — не задан."""
    usage = _file_int(BROWSER_MEMORY_CGROUP_V2_USAGE)
    limit_raw = _file_str(BROWSER_MEMORY_CGROUP_V2_LIMIT)
    if usage:
        # v2 пишет `max` вместо числа, когда предела нет.
        limit = int(limit_raw) if limit_raw.isdigit() else 0
        return usage, limit

    usage = _file_int(BROWSER_MEMORY_CGROUP_V1_USAGE)
    limit = _file_int(BROWSER_MEMORY_CGROUP_V1_LIMIT)
    # v1 без предела пишет не ноль, а «почти вся память машины» — число порядка
    # 2^63. Отдавать его наружу как предел бессмысленно.
    if limit > (1 << 50):
        limit = 0
    return usage, limit


def _chromium_memory() -> tuple[int, int, int]:
    """(PSS, RSS, число процессов) по всем процессам браузера."""
    pss_total = 0
    rss_total = 0
    count = 0

    for process in psutil.process_iter(['name']):
        name = (process.info.get('name') or '').lower()
        if not any(marker in name for marker in BROWSER_MEMORY_PROCESS_NAMES):
            continue
        try:
            info = process.memory_full_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Процессы Chromium живут и умирают постоянно (одна вкладка — один
            # рендерер): исчезнувший между обходом и замером — норма, не сбой.
            continue

        count += 1
        rss_total += info.rss
        # PSS есть только на Linux; на всякий случай откатываемся к RSS.
        pss_total += getattr(info, 'pss', info.rss)

    return pss_total, rss_total, count


def _file_int(path: str) -> int:
    value = _file_str(path)
    return int(value) if value.isdigit() else 0


def _file_str(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ''


def _mb(value: int) -> float:
    return round(value / 1024 / 1024, 1)

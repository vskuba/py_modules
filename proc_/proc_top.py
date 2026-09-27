"""Тяжёлые процессы машины: резидентная память из `/proc`, без `ps`.

Вопрос «кто сожрал память» после `proc_mem_stat` — второй половинкой того же
зонда: сводка сказала, что свободно нечего, а кто виноват — не сказала.
`ps aux --sort=-rss` агенту негоден так же, как `free`: колонки под каждую
версию procps — отдельная работа разбора, а `VmRSS` в `/proc/<pid>/statm` —
второе число строки, читается напрямую.

Рядом с `gpu_vram_procs` модуль стоит осознанно, но не сливается с ним:
резидентная память и видеопамять — разные весы, у процесса бывает и то, и
другое, и смешивать их в одну выдачу значит плодить вопрос «это какие байты».
"""
import os

# Единица учёта страниц: спрашиваем у ядра, а не помним числом — на
# экзотических сборках она не обязана быть кратной мегабайту.
_PROC_PAGE = None


def proc_top_mem(limit: int = 10) -> list:
    """Назвать процессы-держатели резидентной памяти: тяжёлые — первыми.

    Args:
        limit: сколько процессов вернуть.

    Returns:
        [{'pid': int, 'name': str, 'rss': int}] — байты, по убыванию; пустой
        список — не Linux.

    ⚠ `rss` считает **общие страницы каждому владельцу**: библиотечные
    мегабайты приплюсовываются всем, кто их маппит, и сумма топа больше
    памяти машины. Уникальный вес — smaps, а он дорог на каждом круге.
    """
    page = _page_size()
    rows = []
    try:
        pids = [d for d in os.listdir('/proc') if d.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f'/proc/{pid}/statm', 'r', encoding='utf-8') as handle:
                rss = int(handle.read().split()[1]) * page
            with open(f'/proc/{pid}/comm', 'r', encoding='utf-8') as handle:
                name = handle.read().strip()
        except (OSError, IndexError, ValueError):
            continue
        rows.append({'pid': int(pid), 'name': name, 'rss': rss})
    rows.sort(key=lambda row: -row['rss'])
    return rows[:max(1, limit)]


# ── детали реализации ──

def _page_size() -> int:
    global _PROC_PAGE
    if _PROC_PAGE is None:
        try:
            _PROC_PAGE = os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError):
            _PROC_PAGE = 4096
    return _PROC_PAGE


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='процессы-держатели резидентной памяти, тяжёлые первыми; '
                    'вывод — JSON.')
    ap.add_argument('-n', '--limit', type=int, default=10, help='сколько процессов')
    ns = ap.parse_args()
    rows = proc_top_mem(ns.limit)
    print(json.dumps(rows, ensure_ascii=False))
    raise SystemExit(0 if rows else 1)

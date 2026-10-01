"""Память машины целиком: сколько есть, сколько осталось, чем занято.

`free -h` человек читает глазами, агент — нет: разбор его колонок под каждую
версию procps — отдельная работа, а спрашивается всегда одно и то же: сколько
свободно **сейчас** и можно ли совать следующую модель. Ответ лежит в
`/proc/meminfo` в килобайтах и без колонок — читается шестью строками, без
шелла и без зависимости от версии утилит.

Процессы-держатели — половина `proc_top_mem`: этот модуль отвечает на «сколько
всего», а не на «кто».
"""

# Поля `/proc/meminfo`, которые снимаем; имена ответа — без мемфисовской
# верблюжести, но узнаваемые: то же слово, что в файле, только с нижним
# подчёркиванием.
_PROC_MEM_FIELDS = {'MemTotal': 'mem_total', 'MemAvailable': 'mem_available',
                    'SwapTotal': 'swap_total', 'SwapFree': 'swap_free'}


def proc_mem_stat() -> dict:
    """Сводка памяти машины из `/proc/meminfo`: есть, доступно, занято, своп.

    Returns:
        {'mem_total': int, 'mem_available': int, 'mem_used': int,
        'swap_total': int, 'swap_free': int} — байты; пустой словарь —
        не Linux (на macOS файла нет). `mem_used` — total минус available,
        то есть то, что реально отнято у дела, а не «не пусто».

    ⚠ `MemAvailable`, а не `MemFree`, — свободное мерило: кэш страниц
    отдаётся под дело, и «свободно» по `MemFree` пугает нулём там, где
    половина машины — готовая к отдаче кэшированная мелочь.
    """
    out = {}
    try:
        with open('/proc/meminfo', 'r', encoding='utf-8') as handle:
            for line in handle:
                key, _, rest = line.partition(':')
                if key in _PROC_MEM_FIELDS:
                    out[_PROC_MEM_FIELDS[key]] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {}
    if 'mem_total' in out:
        out['mem_used'] = out['mem_total'] - out.get('mem_available', 0)
    return out


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='сводка памяти машины из /proc/meminfo; вывод — JSON.')
    ns = ap.parse_args()
    result = proc_mem_stat()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result else 1)

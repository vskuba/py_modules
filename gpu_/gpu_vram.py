"""Видеопамять сейчас: сколько занято, сколько свободно, кем занято.

Вопрос «влезет ли модель» и вопрос «кто сожрал всю память» — это один зонд,
и до сих пор каждый раз он писался руками: `nvidia-smi`, глазами в таблицу.
Для агента таблица неудобна дважды: числа в ней с единицами измерения
(парсить — сочинять заново), а процессы показывает отдельным запросом
(`--query-compute-apps`), о котором в момент отладки просто забывают.

Обе половины в одном модуле сознательно: у них один транспорт, один формат
CSV и одна ловушка — GB10 фермы счётчики платы **не отдаёт вовсе**, ноль
там не «пусто», а «драйвер молчит»; живые цифры держит `--query-compute-apps`.
"""
import subprocess

# Потолок короткий: nvidia-smi отвечает с драйвера мгновенно; длинный потолок
# только скрывает, что машина не отвечает вовсе.
GPU_VRAM_TIMEOUT = 30.0

# Имя модели — поле с возможными запятыми, поэтому оно в CSV последним:
# числа до него разбиваются надёжно, а хвост остаётся целым.
GPU_VRAM_QUERY = 'index,memory.total,memory.used,memory.free,utilization.gpu,name'
GPU_PROCS_QUERY = 'pid,used_memory,process_name'


def gpu_vram_stat(gpu: int = None, *, host: str = '',
                  timeout: float = GPU_VRAM_TIMEOUT) -> dict:
    """Снять состояние видеопамяти всех плат: занято, свободно, загрузка.

    Args:
        gpu: оставить только эту плату (номер из ответа); пусто — все.
        host: цель в формате `ssh_x`; пусто — своя машина.
        timeout: потолок секунд; исчерпан — `code` = -1.

    Returns:
        {'code': rc, 'gpus': [{'index': int, 'total_mib': int, 'used_mib': int,
        'free_mib': int, 'util': int, 'name': str}], 'error': str}.

    ⚠ На GB10 (единый пул памяти) счётчики платы — **нули**: не «пусто»,
    а «драйвер не показывает». Реальный вес держателей — `gpu_vram_procs`,
    а сколько во всём пуле — `proc_/proc_mem.py`.
    """
    query = (f'nvidia-smi --query-gpu={GPU_VRAM_QUERY} '
             '--format=csv,noheader,nounits')
    raw = _run(query, host, timeout)
    gpus = []
    for line in (raw['output'] or '').splitlines():
        parts = [p.strip() for p in line.split(', ', 5)]
        if len(parts) < 6:
            continue
        entry = {'index': _int(parts[0]), 'total_mib': _int(parts[1]),
                 'used_mib': _int(parts[2]), 'free_mib': _int(parts[3]),
                 'util': _int(parts[4]), 'name': parts[5]}
        if gpu is None or entry['index'] == gpu:
            gpus.append(entry)
    raw['gpus'] = gpus
    return raw


def gpu_vram_procs(*, host: str = '', timeout: float = GPU_VRAM_TIMEOUT) -> dict:
    """Кто держит видеопамять: процессы с pid и весом на текущий миг.

    Args:
        host: цель в формате `ssh_x`; пусто — своя машина.
        timeout: потолок секунд; исчерпан — `code` = -1.

    Returns:
        {'code': rc, 'procs': [{'pid': int, 'used_mib': int, 'name': str}],
        'error': str}.

    ⚠ Процессы из контейнера видны **с pid своей песочницы**: чтобы понять,
    какой это контейнер, pid нужно сверять с `/proc/<pid>/cgroup` на том же
    хосте. Контейнер с пробросом без pid-пространства показан не будет вовсе.
    """
    query = (f'nvidia-smi --query-compute-apps={GPU_PROCS_QUERY} '
             '--format=csv,noheader,nounits')
    raw = _run(query, host, timeout)
    procs = []
    for line in (raw['output'] or '').splitlines():
        parts = [p.strip() for p in line.split(', ', 2)]
        if len(parts) < 3:
            continue
        procs.append({'pid': _int(parts[0]), 'used_mib': _int(parts[1]),
                      'name': parts[2]})
    raw['procs'] = procs
    return raw


# ── детали реализации ──

def _run(query: str, host: str, timeout: float) -> dict:
    """Запрос к nvidia-smi: свой subprocess или поход через ssh_x."""
    if host:
        from ssh_.ssh_x import ssh_x
        return ssh_x(host, query, timeout=timeout)
    try:
        r = subprocess.run(query.split(), capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return {'code': r.returncode, 'output': r.stdout, 'error': r.stderr}
    except FileNotFoundError:
        return {'code': -1, 'output': '',
                'error': 'nvidia-smi не найден — здесь нет драйвера NVIDIA'}
    except subprocess.TimeoutExpired:
        return {'code': -1, 'output': '',
                'error': f'nvidia-smi не ответил за {timeout:g} c'}


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='состояние видеопамяти: --procs покажет держателей; '
                    'вывод — JSON.')
    ap.add_argument('--gpu', type=int, default=None, help='оставить плату N')
    ap.add_argument('--procs', action='store_true', help='процессы вместо плат')
    ap.add_argument('--host', default='', help='цель в формате ssh_x')
    ap.add_argument('--timeout', type=float, default=GPU_VRAM_TIMEOUT)
    ns = ap.parse_args()
    if ns.procs:
        out = gpu_vram_procs(host=ns.host, timeout=ns.timeout)
    else:
        out = gpu_vram_stat(ns.gpu, host=ns.host, timeout=ns.timeout)
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0 if out['code'] == 0 else 1)

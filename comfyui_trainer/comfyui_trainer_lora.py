"""
Лора персоны обучением на ферме: конфиг трейнера из параметров → прогон на
ферме → лора снята в проект.

Драйвер ничего не знает про девушек: он получает папку кадров на ферме (рядом
с кадрами — .txt сайдкары с именем персоны), путь к базе, пути к vae и
текст-энкодерам и параметры обучения. Трейнер на ферме — ai-toolkit (run.py):
он читает родной comfy-e4m3 чекпойнт базы и держит её ОДНОЙ квантованной
копией (quantize) — второй копии в единой памяти GB10 нет. Кадры перед шагами
кэшируются в латенты (cache_latents_to_disk), энкодеры после кэша выгружаются.

Ферма производит, проект хранит: готовый <name>.safetensors снимается в --out
и стирается на ферме, как кадры в comfy_gen.

Память: задача, влезшая в память только что, валит ферму — прежде чем гнать
прогон, драйвер смотрит MemAvailable фермы и не шлёт, если не влезает.
"""
import argparse
import subprocess
import time
from pathlib import Path

import yaml


COMFYUI_TRAINER_TIMEOUT = 36000.0  # 4000 шагов на запечатках — часы, не минуты
COMFYUI_TRAINER_MEM_MARGIN = 4.0   # ГиБ запаса: на живой остаток соседей-процессов
# Потолок на короткий поход к ферме (положить конфиг, спросить память, снять
# лору). Сам трейн этим не ограничен — у него свой COMFYUI_TRAINER_TIMEOUT;
# здесь речь о ssh, который без потолка висит до TCP-таймаута ядра.
COMFYUI_TRAINER_SSH_TIMEOUT = 600.0


def comfyui_trainer_lora(name, dataset, *, base, vae, rank, alpha,
                         lr, steps, seed, out, farm_ssh, farm_container,
                         toolkit='/opt/ComfyUI/tools/ai-toolkit',
                         python='/opt/ComfyUI/tools/aitk/bin/python',
                         output_dir='/opt/ComfyUI/output/train', timeout=None):
    """Один прогон обучения лоры; возвращает путь в проекте.

    name — имя лоры (папка/файл у трейнера и файл в --out), dataset — папка
    кадров с сайдкарами НА ФЕРМЕ, base — путь к чекпойнту базы на ферме.
    rank/alpha/lr/steps/seed — явные числа от вызывающего. Прогон блокирует
    надолго: пока run.py пишет шаги, здесь ждём (timeout по умолчанию — часы).
    """
    cfg = {
        'job': 'extension',
        'config': {
            'name': name,
            'process': [{
                'type': 'sd_trainer',
                'training_folder': output_dir,
                'device': 'cuda:0',
                'network': {'type': 'lora', 'linear': rank, 'linear_alpha': alpha},
                'save': {'dtype': 'float16', 'save_every': steps,
                         'max_step_saves_to_keep': 1},
                'datasets': [{'folder_path': dataset, 'caption_ext': 'txt',
                             'caption_dropout_rate': 0.05, 'shuffle_tokens': False,
                             'cache_latents_to_disk': True,
                             'resolution': [512, 768, 1024]}],
                'train': {
                    'batch_size': 1, 'steps': steps,
                    'gradient_accumulation_steps': 1, 'train_unet': True,
                    'train_text_encoder': False, 'gradient_checkpointing': True,
                    'noise_scheduler': 'flowmatch', 'optimizer': 'adamw8bit',
                    'lr': lr, 'dtype': 'bf16', 'seed': seed,
                    'disable_sampling': True,
                    'ema_config': {'use_ema': True, 'ema_decay': 0.99},
                },
                'model': {'name_or_path': base, 'is_flux': True,
                          'quantize': True, 'vae_path': vae},
            }],
        },
        'meta': {'name': name, 'version': '1.0'},
    }
    yml = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    remote_cfg = f'{toolkit}/config/{name}.yaml'
    subprocess.run([*_ssh(farm_ssh), f'docker exec -i {farm_container} '
                    f'bash -c "cat > {remote_cfg}"'],
                   input=yml.encode(), check=True, capture_output=True,
                   timeout=COMFYUI_TRAINER_SSH_TIMEOUT)
    # единая память GB10: не влезает с запасом — отказ, не отправляя
    free = float(_sh(farm_ssh, farm_container,
                     'grep MemAvailable /proc/meminfo | tr -cd 0-9\\n').strip()) / 2**20
    need = COMFYUI_TRAINER_MEM_MARGIN
    if free < need:
        raise RuntimeError(f'на ферме свободно {free:.0f} ГиБ, прогону надо '
                           f'≥{need:.0f} — не отправляю')
    if 'жив' in _sh(farm_ssh, farm_container,
                    f"pgrep -f '[r]un.py config/{name}.yaml' >/dev/null "
                    f"&& echo жив || echo нет"):
        print(f'трейнер {name} уже жив на ферме — не запускаю второй')
    else:
        _sh(farm_ssh, farm_container,
            f'cd {toolkit} && nohup {python} run.py config/{name}.yaml '
            f'> /tmp/{name}.log 2>&1 & echo запущено')
    deadline = time.monotonic() + (timeout or COMFYUI_TRAINER_TIMEOUT)
    while True:  # лог трейнера — наш /history: жив, пишет шаги, дошёл до конца
        found = _sh(farm_ssh, farm_container,
                    f'ls {output_dir}/{name}/*.safetensors 2>/dev/null; '
                    f"pgrep -f '[r]un.py config/{name}.yaml' >/dev/null "
                    f'&& echo жив || echo нет')
        lines = found.split()
        if any(l.endswith('.safetensors') for l in lines):
            remote = [l for l in lines if l.endswith('.safetensors')][-1]
            break
        if 'нет' in lines:  # трейнер умер, не дойдя до сохранения
            raise RuntimeError(_sh(farm_ssh, farm_container,
                                   f'tail -c 2000 /tmp/{name}.log'))
        if time.monotonic() > deadline:
            raise TimeoutError('трейнер жив, но не дошёл до весов за '
                               f'{timeout or COMFYUI_TRAINER_TIMEOUT} с')
        time.sleep(30)
    return _pull(farm_ssh, farm_container, remote, out, name)


def _ssh(host):
    """`ssh` с адресом в аргументы команды; адрес может нести флаги.

    ⚠ Строка `host` разбирается по пробелам, а не подставляется целиком. Иначе
    вызывающему негде передать `-i ключ` и `-o …`, а они обязательны там, где
    своего `~/.ssh/config` нет или он чужой по правам: внутри контейнера процесс
    работает от root, а проброшенный конфиг принадлежит человеку снаружи — ssh
    отвергает такой файл ДО того, как посмотрит на флаги.
    """
    return ['ssh', *str(host).split()]


def _sh(host, container, sh):
    return subprocess.run([*_ssh(host), f'docker exec {container} sh -c "{sh}"'],
                          capture_output=True, text=True, check=True,
                          errors='replace',
                          timeout=COMFYUI_TRAINER_SSH_TIMEOUT).stdout


def _pull(host, container, remote, out, name):
    """Лора фермы — в проект под именем персоны и стерта на ферме.

    ⚠ `capture_output` и `stdout` вместе НЕ ЖИВУТ: `subprocess.run` бросает
    `ValueError: stdout and stderr arguments may not be used with
    capture_output`. Забор при этом падал ВСЕГДА, а `open(p, "wb")` успевал
    создать пустой файл — и он выглядел как готовая лора. Одно обучение
    (2 ч 17 мин фермы) так и пропало: пустышку приняли за веса, а папку прогона
    следом стёрли.

    Поэтому здесь только `stderr`, а вывод идёт прямо в файл. И папка прогона
    стирается ТОЛЬКО когда файл непустой: пока весов нет, они должны остаться
    на ферме — это единственное место, откуда их ещё можно забрать.
    """
    p = f'{out}/{name}.safetensors'
    with open(p, 'wb') as f:
        got = subprocess.run([*_ssh(host), f'docker exec {container} cat {remote}'],
                             check=True, stderr=subprocess.PIPE, stdout=f,
                             timeout=COMFYUI_TRAINER_SSH_TIMEOUT)
    if not Path(p).stat().st_size:
        raise RuntimeError(f'лора снялась пустой: {remote} '
                           f'({got.stderr.decode(errors="replace").strip()})')
    _sh(host, container, f'rm -rf {remote.rsplit("/", 1)[0]}')  # папка прогона
    return p


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Обучить лору персоны на ферме (ai-toolkit, одна квантованная база).')
    ap.add_argument('--name', required=True, help='имя лоры (файл в --out)')
    ap.add_argument('--dataset', required=True, help='папка кадров с сайдкарами на ферме')
    ap.add_argument('--base', required=True, help='чекпойнт базы на ферме')
    ap.add_argument('--vae', required=True, help='ae.safetensors на ферме')
    ap.add_argument('--rank', type=int, default=16)
    ap.add_argument('--alpha', type=int, default=16)
    ap.add_argument('--lr', type=float, default=4e-4)
    ap.add_argument('--steps', type=int, default=4000)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--out', required=True, help='каталог лор в проекте')
    ap.add_argument('--farm-ssh', required=True)
    ap.add_argument('--farm-container', required=True)
    ns = ap.parse_args()
    Path(ns.out).mkdir(parents=True, exist_ok=True)
    try:
        print(comfyui_trainer_lora(ns.name, ns.dataset, base=ns.base, vae=ns.vae,
                                  rank=ns.rank,
                                  alpha=ns.alpha, lr=ns.lr, steps=ns.steps,
                                  seed=ns.seed, out=ns.out, farm_ssh=ns.farm_ssh,
                                  farm_container=ns.farm_container))
    except (RuntimeError, OSError, subprocess.CalledProcessError) as err:
        raise SystemExit(f'ошибка: {err}')

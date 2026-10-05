"""Жив ли трейнер на ферме: патчи и база на месте — одной командой.

ai-toolkit стоит в контейнере не под git: пересобрался образ — и тихо слетает
патч энкодеров (transformers 5.5 проглатывает subfolder в from_pretrained и
строит модель из конфига-дефолта: CLIP 512 вместо 768 у чекпойнта, трейн тогда
даёт generic-лицо без внятной ошибки) и симлинки model.safetensors, которые
требует тот же загрузчик. Проверка — два grep по stable_diffusion_model.py да
тест файлов базы; вызывает тот, кто собирается гнать трейн.
"""
import subprocess

COMFYUI_TRAINER_TOOLKIT_PATCH_MARKER = 'config=__import__'
COMFYUI_TRAINER_TOOLKIT_UNPATCHED = 'config_class'
# Потолок на проверочный ssh: несколько grep'ов — секунды, а повисший ssh
# к спящей ферме держит вызывающего до TCP-таймаута ядра.
COMFYUI_TRAINER_TOOLKIT_TIMEOUT = 120.0


def comfyui_trainer_toolkit_check(*, toolkit, base, farm_ssh, farm_container):
    """Вернуть строку вердикта о трейнере: патч на месте / что слетело.

    toolkit — каталог ai-toolkit на ферме, base — собранный
    comfyui_trainer_base каталог базы. Вердикт «цело» — можно гнать трейн;
    иначе — что именно слетело, чтобы чинить до первой ночной сборки, а не
    после утреннего generic-лица.
    """
    sh = subprocess.run(
        ['ssh', *str(farm_ssh).split(), f'docker exec -i {farm_container} bash -s'],
        input=(f"echo $(grep -ac '{COMFYUI_TRAINER_TOOLKIT_PATCH_MARKER}' "
               f"{toolkit}/toolkit/stable_diffusion_model.py) "
               f"$(grep -ac '{COMFYUI_TRAINER_TOOLKIT_UNPATCHED}' "
               f"{toolkit}/toolkit/stable_diffusion_model.py) "
               f"$(for d in text_encoder text_encoder_2; do test -e {base}/$d/model.safetensors "
               f"&& echo ok; done | wc -l)\n"),
        capture_output=True, text=True, check=True, errors='replace',
        timeout=COMFYUI_TRAINER_TOOLKIT_TIMEOUT).stdout.split()
    patched, unpatched, links = sh[0], sh[1], sh[2]
    bad = []
    if patched != '2':
        bad.append(f'патч энкодеров ({COMFYUI_TRAINER_TOOLKIT_PATCH_MARKER}) в файле '
                   f'{patched}/2 — без него from_pretrained строит модель из конфига-дефолта')
    if unpatched != '0':
        bad.append(f'непропатченных мест ({COMFYUI_TRAINER_TOOLKIT_UNPATCHED}) {unpatched} '
                   f'— from_pretrained без config= строит модель из конфига-дефолта')
    if links != '2':
        bad.append(f'симлинков model.safetensors в базе {links}/2 — загрузчик их требует')
    return ('трейнер цел: патч энкодеров 2/2, симлинки базы 2/2'
            if not bad else 'слетело: ' + '; '.join(bad))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='Проверить, что трейнер на ферме цел (патч энкодеров, база).')
    ap.add_argument('--toolkit', default='/opt/ComfyUI/tools/ai-toolkit')
    ap.add_argument('--base', default='/opt/ComfyUI/tools/flux1-diffusers')
    ap.add_argument('--farm-ssh', required=True)
    ap.add_argument('--farm-container', required=True)
    ns = ap.parse_args()
    try:
        print(comfyui_trainer_toolkit_check(toolkit=ns.toolkit, base=ns.base,
                                            farm_ssh=ns.farm_ssh,
                                            farm_container=ns.farm_container))
    except (RuntimeError, OSError, subprocess.CalledProcessError) as err:
        raise SystemExit(f'ошибка: {err}')

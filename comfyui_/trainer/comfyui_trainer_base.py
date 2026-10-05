"""Диффузерс-база для трейнера из монолита: один safetensors → папка базы на ферме.

ai-toolkit читает базу папкой diffusers (transformer/ text_encoder/ text_encoder_2/
vae/ tokenizer/ tokenizer_2/ scheduler/), а чекпойнты у ComfyUI — одиночными
файлами с comfy-именами ключей (qkv слитый, индексы ldm-смещённые). Функция
загоняет монолит в diffusers-раскладку прямо на ферме: тела ключей маппятся
точным совпадением с named_parameters пустой модели, разметка имён (qkv-сплиты
с явными размерами, block_N→resnets.N-1, attn_1→attentions.0, reshape под
conv1×1) — в тексте конвертера; разошлось — молчать не должен, SystemExit
показывает, где именно. Веса остаются как были (fp8) — cast делает загрузчик.

Скрипт-конвертер живёт здесь текстом и на ферме после прогона стирается:
ферма — производство, не склад.
"""
import subprocess

COMFYUI_TRAINER_BASE_REPO = ('https://huggingface.co/camenduru/FLUX.1-dev-diffusers'
                             '/resolve/main')
COMFYUI_TRAINER_BASE_SCRIPT = '''import json
import re
import struct
import urllib.request
from pathlib import Path

import torch
from safetensors import safe_open
from safetensors.torch import save_file

SRC = '__SRC__'
DST = Path('__DST__')
REPO = '__REPO__'

with open(SRC, 'rb') as f:
    n = struct.unpack('<Q', f.read(8))[0]
    HDR = {k: v for k, v in json.loads(f.read(n)).items() if k != '__metadata__'}
KEYS = list(HDR)
sh = lambda k: HDR[k]['shape']

dl = sorted({int(re.search(r'double_blocks\\.(\\d+)\\.', k).group(1)) for k in KEYS if 'double_blocks' in k})
sl = sorted({int(re.search(r'single_blocks\\.(\\d+)\\.', k).group(1)) for k in KEYS if 'single_blocks' in k})

from diffusers import FluxTransformer2DModel, AutoencoderKL, FluxPipeline
from transformers import CLIPTextModel, T5EncoderModel, CLIPVisionModel
from transformers import CLIPConfig, T5Config

tf = FluxTransformer2DModel(
    num_layers=len(dl), num_single_layers=len(sl),
    attention_head_dim=sh('model.diffusion_model.double_blocks.0.img_attn.qkv.weight')[1] // 24,
    num_attention_heads=24,
    in_channels=sh('model.diffusion_model.img_in.weight')[1],
    joint_attention_dim=sh('model.diffusion_model.txt_in.weight')[1],
    pooled_projection_dim=sh('model.diffusion_model.vector_in.in_layer.weight')[1],
    guidance_embeds=True)

R = {
    'img_mod.lin': (['norm1'], None), 'txt_mod.lin': (['norm1_context'], None),
    'img_attn.norm.query_norm': (['attn.norm_q'], None), 'img_attn.norm.key_norm': (['attn.norm_k'], None),
    'txt_attn.norm.query_norm': (['attn.norm_added_q'], None), 'txt_attn.norm.key_norm': (['attn.norm_added_k'], None),
    'img_attn.qkv': (['attn.to_q', 'attn.to_k', 'attn.to_v'], (3072, 3072, 3072)),
    'txt_attn.qkv': (['attn.add_q_proj', 'attn.add_k_proj', 'attn.add_v_proj'], (3072, 3072, 3072)),
    'img_attn.proj': (['attn.to_out.0'], None), 'txt_attn.proj': (['attn.to_add_out'], None),
    'img_mlp.0': (['ff.net.0.proj'], None), 'img_mlp.2': (['ff.net.2'], None),
    'txt_mlp.0': (['ff_context.net.0.proj'], None), 'txt_mlp.2': (['ff_context.net.2'], None),
    'norm.query_norm': (['attn.norm_q'], None), 'norm.key_norm': (['attn.norm_k'], None),
    'modulation.lin': (['norm.linear'], None),
    'linear1': (['attn.to_q', 'attn.to_k', 'attn.to_v', 'proj_mlp'], (3072, 3072, 3072, 12288)),
    'linear2': (['proj_out'], None)}
TOP = {
    'img_in': (['x_embedder'], False), 'txt_in': (['context_embedder'], False),
    'time_in.in_layer': (['time_text_embed.timestep_embedder.linear_1'], False),
    'time_in.out_layer': (['time_text_embed.timestep_embedder.linear_2'], False),
    'vector_in.in_layer': (['time_text_embed.text_embedder.linear_1'], False),
    'vector_in.out_layer': (['time_text_embed.text_embedder.linear_2'], False),
    'guidance_in.in_layer': (['time_text_embed.guidance_embedder.linear_1'], False),
    'guidance_in.out_layer': (['time_text_embed.guidance_embedder.linear_2'], False),
    'final_layer.adaLN_modulation.1': (['norm_out.linear'], False),
    'final_layer.linear': (['proj_out'], False)}

def get(path):
    return json.loads(urllib.request.urlopen(f'{REPO}/{path}', timeout=20).read())

def convert(pfx, rules, tgt, out, cfg, subs_=None):
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'diffusion_pytorch_model.safetensors').exists():
        print(out.name, 'уже готово'); return
    srcs = {k[len(pfx):]: k for k in KEYS if k.startswith(pfx)}
    got = {}
    with safe_open(SRC, framework='pt') as f:
        for body, src in srcs.items():
            m = re.match(r'(double|single)_blocks\\.(\\d+)\\.(.*)', body)
            core, tail = body.rsplit('.', 1)
            if m:
                pb = f"{'transformer' if m.group(1) == 'double' else 'single_transformer'}_blocks.{m.group(2)}."
                k = m.group(3).rsplit('.', 1)[0]
                if k not in R:
                    raise SystemExit(f'правило не найдено: {body}')
                dsts = [pb + d for d in R[k][0]]
                sizes = R[k][1]
            elif core in TOP:
                dsts = list(TOP[core][0])
                sizes = TOP[core][1]
            elif subs_ is not None:
                dsts, sizes = [subs_(body)], False
            elif body in rules:
                dsts, sizes = rules[body]
            else:
                dsts, sizes = [body], False
            t = f.get_tensor(src)
            parts = [p.clone() for p in torch.split(t, sizes, 0)] if sizes else [t] * len(dsts)
            for d, part in zip(dsts, parts):
                cand = [p for p in tgt if p == d or p.startswith(d + '.') or p.endswith('.' + d)]
                full = next((p for p in cand if p.rsplit('.', 1)[-1] == body.rsplit('.', 1)[-1]), cand[0] if cand else None)
                if full is None or full in got:
                    if full is None and rules is not R:
                        print('лишний (не параметр модели):', body); continue
                    raise SystemExit(f'нет параметра для {body} -> {dsts}')
                if tuple(tgt[full].shape) != tuple(part.shape):
                    if part.numel() == tgt[full].numel():
                        part = part.reshape(tgt[full].shape)
                    else:
                        raise SystemExit(f'форма {body}->{full}: {tuple(part.shape)} vs {tuple(tgt[full].shape)}')
                got[full] = part
    miss = set(tgt) - set(got)
    if miss:
        raise SystemExit(f'не покрыто {len(miss)}, напр. {sorted(miss)[:3]}')
    (out / 'config.json').write_text(json.dumps(cfg, indent=2))
    save_file(got, out / 'diffusion_pytorch_model.safetensors')
    print(out.name, len(got), 'параметров')

def cd(c):
    return dict(c.to_dict() if hasattr(c, 'to_dict') else c)

def subs(body):
    m = re.match(r'^(encoder|decoder)\\.(up|down)\\.(\\d+)\\.(.*)$', body)
    if m:
        side, ud, i, rest = m.groups()
        i = 3 - int(i) if ud == 'up' else int(i)
        rest = rest.replace('block.', 'resnets.').replace('nin_shortcut', 'conv_shortcut')
        body = f'{side}.{ud}_blocks.{i}.' + rest
    for pat, rep in [(r'\\.mid\\.block_(\\d+)\\.', lambda m: f'.mid_block.resnets.{int(m.group(1)) - 1}.'),
                     (r'\\.mid\\.attn_\\d+\\.norm\\b', '.mid_block.attentions.0.group_norm'),
                     (r'\\.mid\\.attn_\\d+\\.([qkv])\\b', r'.mid_block.attentions.0.to_\\1'),
                     (r'\\.mid\\.attn_\\d+\\.proj_out', '.mid_block.attentions.0.to_out.0'),
                     (r'\\.norm_out\\.', '.conv_norm_out.'),
                     (r'\\.up_blocks\\.(\\d+)\\.upsample\\.', r'.up_blocks.\\1.upsamplers.0.'),
                     (r'\\.down_blocks\\.(\\d+)\\.downsample\\.', r'.down_blocks.\\1.downsamplers.0.')]:
        body = re.sub(pat, rep, body)
    return body.replace('nin_shortcut', 'conv_shortcut')

vcfg = get('vae/config.json')
vcfg.pop('_class_name', None); vcfg.pop('_diffusers_version', None)
ccfg = get('text_encoder/config.json')
tcfg = get('text_encoder_2/config.json')
vae = AutoencoderKL(**{k: v for k, v in vcfg.items() if not k.startswith('_')})
clip = CLIPTextModel(CLIPConfig(**{k: v for k, v in ccfg.items() if not k.startswith('_')}))
t5 = T5EncoderModel(T5Config(**{k: v for k, v in tcfg.items() if not k.startswith('_')}))

convert('model.diffusion_model.', R, dict(tf.named_parameters()), DST / 'transformer', cd(tf.config))
convert('text_encoders.clip_l.transformer.', {}, dict(clip.named_parameters()), DST / 'text_encoder',
        cd(clip.config))
convert('text_encoders.t5xxl.transformer.', {}, dict(t5.named_parameters()), DST / 'text_encoder_2',
        {k: v for k, v in cd(t5.config).items() if k != '_name_or_path'})
convert('vae.', {}, dict(vae.named_parameters()), DST / 'vae', {**vcfg, '_class_name': 'AutoencoderKL'}, subs)

(DST / 'scheduler').mkdir(parents=True, exist_ok=True)
(DST / 'scheduler/scheduler_config.json').write_text(json.dumps(get('scheduler/scheduler_config.json'), indent=2))

RAW = ['tokenizer/vocab.json', 'tokenizer/merges.txt', 'tokenizer/tokenizer_config.json',
       'tokenizer/special_tokens_map.json', 'tokenizer_2/tokenizer_config.json',
       'tokenizer_2/tokenizer.json', 'tokenizer_2/special_tokens_map.json', 'tokenizer_2/spiece.model']
for p in RAW:
    try:
        body = urllib.request.urlopen(f'{REPO}/{p}', timeout=20).read()
    except Exception as e:
        print('нет', p, str(e)[:40]); continue
    (DST / p).parent.mkdir(parents=True, exist_ok=True)
    (DST / p).write_bytes(body)
    print(p, len(body))
# transformers требует model.safetensors — у нас файл лежит как у diffusers
for d in ('text_encoder', 'text_encoder_2'):
    t = DST / d / 'diffusion_pytorch_model.safetensors'
    if t.exists() and not (DST / d / 'model.safetensors').exists():
        (DST / d / 'model.safetensors').symlink_to(t.name)
print('готово:', DST)
'''


def comfyui_trainer_base(src, dst, *, farm_ssh, farm_container,
                        python='/opt/ComfyUI/tools/aitk/bin/python',
                        repo=COMFYUI_TRAINER_BASE_REPO):
    """Собрать diffusers-папку базы из монолита; вернуть собранный каталог.

    src — одиночный safetensors на ферме (comfy-имена ключей), dst — каталог
    базы на ферме же (создаётся; уже готовая часть не переписывается), repo —
    зеркало конфигов/токенизаторов (gated-репозиторий не годится — тут же и
    конфиги, и Tokenizer-файлы качаются без токена). Прогон блокирует вызов на
    минуты: это конверт 17 ГиБ весов, не фоновая задача.
    """
    script = (COMFYUI_TRAINER_BASE_SCRIPT
              .replace('__SRC__', src).replace('__DST__', str(dst))
              .replace('__REPO__', repo))
    r = subprocess.run(['ssh', *str(farm_ssh).split(), f'docker exec -i {farm_container} '
                        f'bash -c "cat > /tmp/comfyui_trainer_base.py"'],
                       input=script.encode(), check=True, capture_output=True)

    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True,
                              check=True, errors='replace').stdout

    out = _run(['ssh', *str(farm_ssh).split(), f'docker exec {farm_container} {python} '
                f'/tmp/comfyui_trainer_base.py'])
    _run(['ssh', *str(farm_ssh).split(), f'docker exec {farm_container} rm -f '
          f'/tmp/comfyui_trainer_base.py'])
    print(out)
    return str(dst)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='Собрать diffusers-папку базы для трейнера из одиночного safetensors.')
    ap.add_argument('src', help='одинокий safetensors на ферме')
    ap.add_argument('dst', help='каталог базы на ферме')
    ap.add_argument('--python', default='/opt/ComfyUI/tools/aitk/bin/python')
    ap.add_argument('--repo', default=COMFYUI_TRAINER_BASE_REPO)
    ap.add_argument('--farm-ssh', required=True)
    ap.add_argument('--farm-container', required=True)
    ns = ap.parse_args()
    try:
        print(comfyui_trainer_base(ns.src, ns.dst, farm_ssh=ns.farm_ssh,
                                   farm_container=ns.farm_container,
                                   python=ns.python, repo=ns.repo))
    except (RuntimeError, OSError, subprocess.CalledProcessError) as err:
        raise SystemExit(f'ошибка: {err}')

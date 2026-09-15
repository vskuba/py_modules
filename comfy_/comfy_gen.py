"""
Пачка кадров персоны: сцена JSON -> ComfyUI-API -> файл + строка манифеста.

Драйвер ничего не знает про девушек и сцены: он исполняет workflow в API-форме
(тот лежит у персоны рядом с scenes.json), подставляя маркеры — __PROMPT__
(текст сцены), __ANCHOR__ (имя загруженного якоря), __SEED__ (номер запуска).
Откуда берётся консистентность лица — PuLID в workflow или обученный LoRA, —
дело workflow, не драйвера.

Якорь приходит в ферму POST /upload/image — драйвер не знает ни хостов, ни
ssh: тот же код гоняет против любой ComfyUI по http. Хозяйский дом — проект:
если ферма передана как farm_ssh/farm_container (значения даёт вызывающий),
готовый результат снимается с фермы в --out и стирается на ней — ферма делает,
проект хранит.

GB10 с единой памятью: задача, влезшая в память только что, роняет всю ферму.
Поэтому workflow несёт верхним ключом `_mem` — примерный аппетит в ГБ. Память на
ферме свободна всегда — что держат её прошлые прогоны, а не соседи: драйвер
обязательно чистит её (`comfy_gen_free`) перед отправкой задачи, и лишь когда
после чистки не хватает `_mem` + запас — отказывает, не отправляя.

Кадры сходят с конвейера без EXIF (exiftool -all=) и с именами-хэшами, как
файлы на сайте: sha256[:32].jpg. Каждая минута провала — seed+1000, это решает
вызвавший, драйвер лишь честно исполняет прогон и пишет score/pass в манифест
построчно — их потом правит ai_face(check).
"""
import argparse
import hashlib
import json
import math
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image

# Прогон тяжёлого workflow (flux + лоры) на ферме — минуты; ждём без стеснения.
COMFY_GEN_TIMEOUT = 900.0

# Сколько ждать живого system_stats после перезапуска контейнера фермы.
COMFY_GEN_FREE_WAIT = 180.0

# Маркеры в API-JSON workflow: значения узлов, которые драйвер подставляет.
COMFY_GEN_MARKERS = ('__PROMPT__', '__ANCHOR__', '__SEED__', '__DENOISE__',
                    '__FACE__', '__DETAIL__', '__WIDTH__', '__HEIGHT__',
                    '__STEPS__', '__LORA__')

# Запас к `_mem` workflow: чужие процессы (рабочая чат-модель) уже учтены в
# ram_free фермы — запас лишь на скачок живого остатка, не на соседей. Запас
# — фактический зазор, оставленный прогоном: у обучения на 15 заплатках он был
# 0.4 ГиБ (free 93.6→0.4) и прогон жил; 4 ГиБ «на глаз» тут не оставляют места
# никакой задаче (свободно максимум 93.7).
COMFY_GEN_MEM_MARGIN = 0.3

# Обучение — те же шаги × градиент-аккумуляция прогонов, оно медленнее инференса:
# полный прогон на 96 заплатках (900–1500 шагов) идёт больше часа — прошлый
# компас 3600 с не дожил бы до SaveLoRA.
COMFY_GEN_TRAIN_TIMEOUT = 7200.0
COMFY_GEN_TRAIN_BUDGET = (512, 512)  # бюджет площади кадра: патч сверх неё жался бы пропорционально;
                                     # flux1-dev-fp8 на 768×1024 (0.79 МП/кадр) не влезает в память
                                     # и падает torch.OutOfMemoryError — см. docs/comfy_gen.md
COMFY_GEN_REMOTE = '/opt/ComfyUI/output'  # каталог готовых файлов внутри контейнера фермы
COMFY_GEN_MODELS = '/opt/ComfyUI/models/loras'  # куда ложится лора-актив на ферме

# Длинная сторона рендера выкройки, px. Выкройка идёт в граф В СВОИХ
# ПРОПОРЦИЯХ: сплющивание в квадрат ломает геометрию лица (flux на 2D RoPE
# считает по нативному аспекту), а овал, растянутый в квадрат и сжатый обратно,
# возвращается чужой формы. Кратность 16 — страйд VAE 8 × патч flux 2.
COMFY_GEN_SWAP_RENDER = 768

# QA кадра с лицом: косинус против якоря не ниже порога — иначе прогон на seed+сдвиг.
COMFY_GEN_QA_PASS = 0.5
COMFY_GEN_QA_RETRY_SEED = 1000
# Детекция сверки: лицо живого кадра — крупное, на det 640 детектор дробит его
# до несерьёзных пикселей и не берёт (замер: на close-up 896×1152 детект 640 —
# 0 лиц, 320 — лицо с det_score 0.85). 320 берёт и крупные, и мелкие лица.
COMFY_GEN_QA_DET = (320, 320)
# (замер 2026-09-14: тот же кадр swap на det 1024 даёт score 0.0 — крой/embedding
# при детекции крупнее дет_size ломаются; сверка полным кадром идёт тем же детектором,
# каким мерилась заплатками — 320.)


def comfy_gen_batch(workflow, scene, out, *, base, persona='', anchor=None,
                    anchor_input='', seed=1, n=1, denoise=1.0, detail=0.22,
                    qa_anchor='', qa_pass=COMFY_GEN_QA_PASS,
                    width=0, height=0, steps=0, lora=1.0,
                    qa_det=COMFY_GEN_QA_DET, farm_ssh='', farm_container='',
                    remote=COMFY_GEN_REMOTE, face='', reuse=False):
    """Прогнать workflow над сценой n раз; вернуть строки манифеста по кадрам.

    Кадры с лицом (scene.face_on) скорятся лицом-якорем `qa_anchor` детекцией
    `qa_det` по порогу `qa_pass`, иначе тот же прогон на
    seed+COMFY_GEN_QA_RETRY_SEED. Порог — параметр, а не константа: на лице в
    сотню пикселей косинус к якорю физически ниже, чем на close-up, и общий 0.5
    заставлял бы каждый прогон вслепую повторяться. `qa_pass=0` — не пересевать.

    workflow — путь к API-JSON; scene — запись scenes.json (id/prompt/nsfw/
    face_on); out — каталог куда класть кадры; base — адрес ComfyUI
    («http://хост:порт»); anchor — файл якорного лица, грузится в input фермы,
    его новое имя подставляется вместо __ANCHOR__ (workflow без якоря его
    просто не содержит); seed — стартовый, i-й кадр — seed+i; перед отправкой
    память фермы чистится `comfy_gen_free` (кэш моделей держит цифру внизу) и
    лишь потом `_mem` (ГБ) сверяется с свободным — не влезает — отказ.
    """
    wf = json.loads(Path(workflow).read_text())
    need = float(wf.pop('_mem', 0))
    comfy_gen_free(base, farm_ssh, farm_container, need, reuse=reuse)
    anchor_name = comfy_gen_upload(anchor, base) if anchor else ''
    face_name = comfy_gen_upload(face, base) if face else ''
    if anchor_input:
        _wf_set(wf, anchor_input, anchor_name or str(anchor))
    rows = []
    for i in range(n):
        body = lambda s: _wf_fill(json.loads(json.dumps(wf)),
                                  {'__PROMPT__': f'{persona}, {scene["prompt"]}'.strip(', '),
                                   '__ANCHOR__': anchor_name,
                                   '__FACE__': face_name,
                                   '__SEED__': str(s),
                                   '__DENOISE__': str(denoise),
                                   '__DETAIL__': str(detail),
                                   '__WIDTH__': str(width),
                                   '__HEIGHT__': str(height),
                                   '__STEPS__': str(steps),
                                   '__LORA__': str(lora)})
        got = _run_one(body(seed + i), scene, out, base, seed + i, need,
                       farm_ssh=farm_ssh, farm_container=farm_container)
        if qa_anchor and scene.get('face_on', True):
            _comfy_gen_qa(got, out, qa_anchor, qa_det, qa_pass)
            if qa_pass and not all(r['pass'] for r in got):
                got = _run_one(body(seed + i + COMFY_GEN_QA_RETRY_SEED), scene,
                               out, base, seed + i + COMFY_GEN_QA_RETRY_SEED, need,
                               farm_ssh=farm_ssh, farm_container=farm_container)
                _comfy_gen_qa(got, out, qa_anchor, qa_det, qa_pass)
        rows.extend(got)
    return rows


def comfy_gen_train(workflow, files, *, base, caption, seed=1, budget=COMFY_GEN_TRAIN_BUDGET,
                    steps=0, out='', farm_ssh='', farm_container='',
                    remote=COMFY_GEN_REMOTE):
    """Обучить LoRA на пачке кадров; вернуть локальные пути готовых лор.

    workflow — API-JSON с LoadImageTextDataSetFromFolder/MakeTrainingDataset/
    TrainLoraNode/SaveLoRA; files — локальные кадры датасета: каждый грузится в
    input-подпапку фермы с именем caption. Габариты доводятся до кратно 16
    (страйд VAE 8 × патч flux 2), а сверх бюджета площади `budget` патч жался бы
    пропорционально — иначе только родной размер: flux на 2D RoPE построен на
    нативном аспекте, и узел сам ведёт список латентов разных форм (multi-res;
    LatentBatch требовал одинаковый размер и калечил пропорции лиц).
    caption — единая подпись датасета (id персоны, не сцены). `steps` — число
    шагов через маркер `__STEPS__`, если граф его несёт: коротким прогоном
    (8–16 шагов) меряют ПИК ПАМЯТИ, не тратя часы на полное обучение. Без
    маркера число шагов остаётся тем, что записано в графе.

    Лора — актив проекта, ферма её не хранит: при заданных farm_ssh/
    farm_container свежий файл снимается с фермы в out (имя — из prefix
    SaveLoRA без счётчика шагов) и стирается на ферме; без farm_* глухо
    остаётся на ферме (зачем актив чужой установки лежит здесь).
    """
    wf = json.loads(Path(workflow).read_text())
    need = float(wf.pop('_mem', 0))
    comfy_gen_free(base, farm_ssh, farm_container, need)
    for n in wf.values():
        if n['_cls'] == 'LoadImageTextDataSetFromFolder':
            n['inputs']['folder'] = caption
    tmp_dir = Path(tempfile.mkdtemp())
    for f in map(Path, files):
        with Image.open(f) as im:
            w0, h0 = im.size
        scale = min(1.0, math.sqrt(budget[0] * budget[1] / (w0 * h0)))
        w, h = max(16, round(w0 * scale / 16) * 16), max(16, round(h0 * scale / 16) * 16)
        if (w, h) == (w0, h0):
            comfy_gen_upload(f, base, subfolder=caption)
        else:
            q = tmp_dir / f.name
            with Image.open(f) as im:
                im.resize((w, h), Image.LANCZOS).save(q)
            comfy_gen_upload(q, base, subfolder=caption)
    run = _wf_fill(json.loads(json.dumps(wf)),
                   {'__PROMPT__': caption, '__ANCHOR__': '', '__SEED__': str(seed),
                    '__DENOISE__': '1.0', '__STEPS__': str(steps)})
    _run_one(run, {'id': 'train', 'nsfw': False}, '', base, seed, need,
               timeout=COMFY_GEN_TRAIN_TIMEOUT)
    if not farm_ssh:
        return []
    prefix = next(n['inputs']['prefix'] for n in wf.values()
                  if n.get('class_type', n.get('_cls')) == 'SaveLoRA')
    sub, stem = str(Path(prefix).parent), Path(prefix).name
    got = []
    for name in (f for f in _farm_sh(farm_ssh, farm_container, f'ls {remote}/{sub}')
                 .split() if f.startswith(stem) and f.endswith('.safetensors')):
        got.append(_farm_pull(farm_ssh, farm_container, remote, f'{sub}/{name}',
                              Path(out) if out else Path('.'), stem + '.safetensors'))
    if got:
        # Паспорт прогона рядом с лорой: без него «чем её учили» не
        # восстанавливается ничем — ни именем файла, ни историей фермы.
        # Ровно эта дыра и обнаружилась на diana_face.safetensors.
        from image_.image_manifest import image_manifest_save
        node = next((n['inputs'] for n in wf.values()
                     if n.get('class_type', n.get('_cls')) == 'TrainLoraNode'), {})
        image_manifest_save(got, Path(out) if out else Path('.'), batch='train',
                            caption=caption, seed=seed, budget=list(budget),
                            steps=steps or node.get('steps'),
                            workflow=Path(workflow).name, dataset=len(files),
                            sources=sorted(Path(f).name for f in files),
                            params={k: node.get(k) for k in
                                    ('rank', 'learning_rate', 'optimizer',
                                     'batch_size', 'grad_accumulation_steps',
                                     'training_dtype', 'lora_dtype')})
    return got


def comfy_gen_swap(photos, workflow, out, *, base, persona='', mode='face',
                   denoise=0.8, detail=0.22, seed=1, qa_det=COMFY_GEN_QA_DET,
                   face='', hf=0, render=COMFY_GEN_SWAP_RENDER, steps=20,
                   lora=1.0, grain=1.0, feather=0.0, fit_full=0.0, parts_ask=True,
                   qa_pass=COMFY_GEN_QA_PASS, reuse=False,
                   farm_ssh='', farm_container=''):
    """Натянуть лицо лоры на готовые фото; вернуть строки манифеста по фото.

    Драйвер — только диспетчер: выкройку лица (`ai_face_crop` с `mode`) грузит
    в граф вместо целого кадра — лицо заполняет разрешение графа, и сэмплинг
    платит за его пиксели целиком (целый кадр, сплющенный в ~512², даёт мыло:
    лицо ~150 px, резкость 1.6 против 172 у ветки, замер 2026-09-15). Рендер
    идёт в ПРОПОРЦИЯХ выкройки (`render` — длинная сторона, кратно 16): квадрат
    растягивает овал и возвращает лицо чужой формы.

    Здесь же: якорь персоны под ракурс кадра (ai_look_anchor), замеры кадра
    (ai_look_probe), seed и приёмка цифрами. Сборка обратно — четырьмя
    стадиями, и порядок у них жёсткий:

    1. выход графа сжимается до масштаба кадра — ДО тона и зерна, иначе
       ресайз сотрёт положенное зерно и вернёт гладкость;
    2. `ai_look_fit` — каналы к межквартильному тону кожи ТЕЛА кадра (не лица
       оригинала: оно чужое и своего тона);
    3. `image_grain_match` — зерно и фокус до настоящей кожи рядом с лицом
       (`grain` — множитель, 1.0 ровно до эталона). Без этой стадии заплатка
       остаётся стерильной, и глаз читает подделку раньше, чем смотрит на черты;
    4. `ai_mask_face` + `ai_face_paste` — вшивка по контуру 106 точек МИНУС
       перекрытия: прядь поперёк щеки и пальцы у подбородка остаются кадром.
       Вне маски кадр цел байт в байт (`ai_look_integrity`).

    `hf` > 0 — частотная склейка поверх этого: `hf` мелких уровней пирамиды
    берут текстуру самого кадра. Для чужого лица замер её отвергал (0.537
    против 0.363 при hf=1, 2026-09-14), и с зерном из стадии 3 она обычно не
    нужна; ось оставлена для кадров той же персоны.

    `parts_ask` — спрашивать ли vision-моделью, что попало в выкройку кроме
    лица (нужен ключ провайдера; без него — пустой список, прогон идёт).
    Провал гейта — тот же прогон на seed+COMFY_GEN_QA_RETRY_SEED внутри
    comfy_gen_batch. Замеры, стадии и вердикты — в строке манифеста.
    """
    from PIL import Image
    from ai.ai_face import ai_face_crop, ai_face_paste, ai_face_score
    from ai.ai_landmark import ai_landmark_dense
    from ai.ai_look import (ai_look_probe, ai_look_prompt, ai_look_parts,
                           ai_look_fit, ai_look_anchor, ai_look_integrity)
    from ai.ai_mask import ai_mask_face
    from image_.image_grain import (image_grain_match, image_grain_measure,
                                   image_grain_ref)
    rows = []
    for p in map(Path, photos):
        with tempfile.TemporaryDirectory() as td:
            crop = Path(td) / (p.stem + '.png')
            cut = ai_face_crop(p, crop, mode=mode, det_size=tuple(qa_det))
            box = cut['box']
            lm = ai_landmark_dense(p, det_size=tuple(qa_det))
            f = Path(face) if face else None
            # Центроид пачки — второй, независимый от ракурса судья: считается
            # по каталогу патчей рядом с якорем (или рядом с файлом-якорем).
            pack = f if f and f.is_dir() else (f.parent if f else None)
            centroid = _swap_centroid(pack) if pack and pack.is_dir() else None
            if f and f.is_dir():  # каталог патчей: якорь — самый чистый под ракурс кадра
                f = Path(ai_look_anchor(sorted(f.glob('*.png')), p,
                                        parts=parts_ask,
                                        cache=str(f / 'anchors.json'))['face'])
            probe = ai_look_probe(p, box)
            # Цель подгонки — не лицо оригинала (оно чужое и своего тона), а
            # настоящая кожа кадра: лицо обязано совпасть с шеей и плечами,
            # иначе «жёлтое лицо» (замер 12677b19: лицо [199,140,118] против
            # тела [204,169,152] — минус 34 по синему).
            # Но полосой «под подбородком» кожу брать нельзя: под ним бывают
            # волосы, и тогда лицо подгоняется под тон ВОЛОС (замер
            # 3095684: цель вышла [72,45,28] — это причёска, лицо ушло в
            # оранжевую тень). Окно кожи ищется тем же инструментом, что и
            # эталон зерна; не нашлось похожего — падаем на старую полосу,
            # и это видно по `skin_part` в манифесте.
            ref = image_grain_ref(p, box, skin=probe['skin'][::-1])
            # Маска нужна ещё до прогона: по ней меряется ЦЕЛЬ фактуры —
            # кожа самого лица оригинала (без волос и перекрытий). Соседняя
            # чистая кожа для этого не годится: у руки нет ни пор, ни веснушек
            # лицевой плотности (замер 3095684: лицо 2.76 сигмы против 2.13 у
            # плеча), и заплатка, дотянутая до плеча, остаётся глаже соседа
            # по кадру — самого лица.
            mpath = Path(td) / (p.stem + '-mask.png')
            msk = ai_mask_face(p, lm['contour'], str(mpath),
                               feather=feather or max(2.0, (box[3] - box[1]) * 0.035))
            # `flat` — окно прошло по цвету и яркости, но фактуры в нём нет:
            # это гладкий фон, а не кожа (замер istockphoto-653141840: стена
            # кухни, сигма 0.16 против 0.86 у лица). Равнять по ней ТОН нельзя —
            # лицо утащило на [208,164,101] и по контуру пошла синяя кромка.
            if ref.get('own'):
                # Кожи рядом не нашлось (крупный портрет — обычное дело):
                # целью тона становится КОЖА ЛИЦА ОРИГИНАЛА. Это верная цель, и
                # подгонку глушить нельзя — генерация уезжает по цвету сильно
                # (замер 2026-09-16: синий канал 58 против 97 у оригинала, лицо
                # жёлто-зелёное). Пятна, которые тут были раньше, шли не от
                # сдвига, а от весовой карты; её порог исправлен в ai_look_fit.
                target = probe
            elif ref['skin_part'] >= 0.5 and not ref.get('flat'):
                target = ai_look_probe(p, ref['box'])
            else:
                # кожи рядом нет — равняемся на тон ТОГО ЛИЦА, которое заменяем:
                # оно заведомо в свете этого кадра, а полоса под подбородком
                # бывает и волосами, и воротником
                target = probe
            parts = ai_look_parts(p, box) if parts_ask else []
            prompt = persona + ai_look_prompt(probe)
            w_r, h_r = _swap_render(box, render)
            got = comfy_gen_batch(workflow, {'id': p.stem, 'prompt': prompt,
                                             'nsfw': False}, out, base=base,
                                  persona=persona, seed=seed, n=1,
                                  denoise=denoise, detail=detail,
                                  anchor=str(crop), face=f or '',
                                  width=w_r, height=h_r, steps=steps, lora=lora,
                                  qa_anchor=str(f or crop), qa_det=qa_det,
                                  qa_pass=qa_pass, reuse=reuse,
                                  farm_ssh=farm_ssh, farm_container=farm_container)
            mp = Path(out) / 'manifest.json'
            man = json.loads(mp.read_text()) if mp.exists() else []
            for r in got:
                gen = Path(out) / r['file']
                bw, bh = box[2] - box[0], box[3] - box[1]
                # Порядок стадий — не косметика, а условие фотореализма:
                # 1) выход графа (рендер w_r×h_r) сжимается до масштаба КАДРА,
                # 2) только потом тон и зерно — положить зерно до сжатия значит
                #    сжать его вместе с картинкой и снова получить гладко.
                patch = Path(td) / (p.stem + '-patch.png')
                with Image.open(gen) as im:
                    im.resize((bw, bh), Image.LANCZOS).save(patch)
                # каналы — до тона КОЖИ ТЕЛА кадра (не лица оригинала: оно чужое
                # и своего тона; замер 12677b19 — минус 34 по синему)
                fit = ai_look_fit(patch, target, patch, full=fit_full)
                # зерно: генератор отдаёт стерильный пиксель, а рядом лежит
                # настоящая кожа с шумом матрицы и следами JPEG. Эталон — то же
                # окно кожи, что дало цель тона: зерно берётся оттуда же
                # (спектр этой камеры), а не синтезируется белым шумом.
                # ЦЕЛЬ — фактура лица оригинала под маской, ИСТОЧНИК спектра —
                # окно чистой кожи: у лица правильная плотность деталей, у окна
                # — чистый шум камеры без структуры, которую нельзя тиражировать.
                want = image_grain_measure(p, box, mask=str(mpath))
                grain_r = image_grain_match(patch, patch, want, ref=str(p),
                                            ref_box=ref['box'], strength=grain)
                # маска — контур по 106 точкам МИНУС перекрытия (посчитана выше,
                # ею же меряли цель фактуры): пряди волос поперёк лица и пальцы
                # у подбородка обязаны остаться кадром, иначе подмена видна
                # раньше, чем само лицо (спор «эллипс или прямоугольник» был
                # спором двух неверных форм: 0.305 и 0.689)
                ai_face_paste(p, patch, gen, box, hf=hf, mask=str(mpath))
                r['gen_score'] = r['score']  # что дал граф до диспетчерской склейки
                r['fit'] = {k: fit[k] for k in ('before', 'after', 'coef')}
                r['paste'] = {'hf': hf, 'render': [w_r, h_r],
                              'feather': round(feather or max(2.0, bh * 0.035), 1)}
                r['mask'] = {k: msk[k] for k in ('cover', 'occluded', 'fallback')}
                r['grain'] = {'want': want['sigma'], 'was': grain_r['before']['sigma'],
                              'now': grain_r['after']['sigma'],
                              'from': grain_r['added'], 'ref': ref['box'],
                              'skin_part': ref['skin_part']}
                r['integrity'] = ai_look_integrity(gen, p, box)
                r['score'] = ai_face_score(gen, f or crop, det_size=tuple(qa_det))
                if centroid:
                    # шкала честная: −0.14 у чужого лица кадра, 0.78 — потолок
                    # самих фотографий персоны (coherence). Один якорь столько
                    # не скажет: в его косинусе сидит ещё и ракурс якоря.
                    r['score_set'] = {
                        'now': ai_face_score(gen, centroid['embedding'],
                                             det_size=tuple(qa_det)),
                        'was': ai_face_score(p, centroid['embedding'],
                                             det_size=tuple(qa_det)),
                        'ceiling': centroid['coherence'], 'n': centroid['n']}
                r['pass'] = r['score'] >= qa_pass
                for m in man:
                    if m['file'] == r['file']:
                        m.update(r)
                        m.update({'mode': mode, 'denoise': denoise,
                                  'detail': detail, 'box': box,
                                  'pose': lm['pose'], 'lora': lora,
                                  'steps': steps,
                                  'prompt': prompt, 'parts': parts,
                                  'face': f.name if f else '',
                                  'probe': {k: probe[k] for k in
                                            ('skin', 'skin_std', 'hair',
                                             'light', 'sharp')}})
                        rows.append(dict(m))
                mp.write_text(json.dumps(man, ensure_ascii=False,
                                         indent=1) + '\n')
    return rows


def comfy_gen_upload(path, base, subfolder=''):
    """Залить файл в input фермы (в подпапку, если задана) через /upload/image."""
    p = Path(path)
    r = httpx.post(f'{base}/upload/image',
                   files={'image': (p.name, p.read_bytes())},
                   data={'subfolder': subfolder, 'overwrite': 'true'}, timeout=60)
    r.raise_for_status()
    name = r.json()['name']
    return name


def comfy_gen_free(base, farm_ssh='', farm_container='', need=0.0, reuse=False):
    """Перед каждой задачей: освободить память фермы, вернуть свободно (ГиБ).

    `reuse` — не выгружать, если свободного уже хватает под `need`: для пачки
    кадров по одному графу перезагрузка модели (17 ГБ с диска) стоит дороже
    всего остального прогона.

    Память на GB10 свободна всегда — цифра ниже есть только потому, что ComfyUI
    держит модели в кэше с прошлых прогонов: POST /free их выгружает до `need`.
    Выгрузила не всё (torch пул не доедает) или ручки нет вовсе — тот же
    инструмент перезапускает контейнер (farm_ssh/farm_container) и ждёт живого
    system_stats; ждать «само освободится» нечего.
    """
    if reuse:
        # Пачкой по одному графу выгрузка стоит дороже, чем экономит: модель
        # весит 17 ГБ и читается с диска заново перед КАЖДЫМ кадром. Если
        # свободного и так хватает — не трогаем кэш. Гейт при этом никуда не
        # девается: не хватило — идём обычным путём и выгружаем.
        try:
            free = _gen_alive(base)
            if free >= need + COMFY_GEN_MEM_MARGIN:
                return free
        except (httpx.HTTPError, TimeoutError):
            pass
    try:
        r = httpx.post(f'{base}/free', json={'unload_models': True,
                                             'free_memory': True}, timeout=30)
        ok = r.is_success
    except httpx.TransportError:
        ok = False
    if ok:
        free = _gen_alive(base)
        if free >= need + COMFY_GEN_MEM_MARGIN:
            return free
    if not farm_ssh:
        raise RuntimeError(f'ферма {base} кэш не выгрузила'
                           + (f': свободно {free:.0f} ГиБ' if ok else ' и не ответила')
                           + f', задаче надо ≥{need + COMFY_GEN_MEM_MARGIN:.0f} — не отправляю')
    subprocess.run(['ssh', farm_ssh, f'docker restart {farm_container}'],
                   capture_output=True, check=True)
    return _gen_alive(base)


def comfy_gen_model(files, base, farm_ssh, farm_container, dest=COMFY_GEN_MODELS):
    """Лору-актив проекта — в models фермы, контейнер рестартнуть и ждать живого.

    LoraLoader ключуется путём: свежий файл под тем же именем без рестарта
    молча генерит прежним лицом (граф тот же, манифест разницы не видит) —
    поэтому именно рестарт контейнера, а не только загрузка файла."""
    for f in files:
        name = Path(f).name
        subprocess.run(['scp', str(f), f'{farm_ssh}:/tmp/{name}'],
                       capture_output=True, check=True)
        subprocess.run(['ssh', farm_ssh, f'docker cp /tmp/{name} {farm_container}:'
                        f'{dest}/{name} && rm /tmp/{name}'],
                       capture_output=True, text=True, check=True)
    subprocess.run(['ssh', farm_ssh, f'docker restart {farm_container}'],
                   capture_output=True, text=True, check=True)
    _gen_alive(base)
    return [Path(f).name for f in files]


def _swap_centroid(folder):
    """Центроид пачки патчей персоны с кэшем рядом; {'embedding','n','coherence'}.

    Центроид — судья вернее одного якоря (ai_face_centroid), но считается он
    детекцией по всем патчам: две сотни файлов на CPU это минуты, а прогон
    swap зовут десятками. Поэтому результат ложится в `centroid.json` рядом с
    патчами и пересчитывается, только когда пачка изменилась (число файлов).
    """
    from ai.ai_face import ai_face_centroid
    files = sorted(Path(folder).glob('*.png'))
    cache = Path(folder) / 'centroid.json'
    if cache.exists():
        got = json.loads(cache.read_text())
        if got.get('files') == len(files):
            return got
    got = ai_face_centroid(files)
    got['files'] = len(files)
    cache.write_text(json.dumps(got, ensure_ascii=False) + '\n')
    return got


def _swap_render(box, long_side):
    """Размер рендера выкройки: её пропорции, длинная сторона `long_side`, /16.

    Кратность 16 — страйд VAE 8 × патч flux 2; аспект сохраняется, потому что
    flux построен на 2D RoPE и нативном соотношении сторон, а овал, растянутый
    в квадрат и сжатый обратно, возвращается лицом другой формы.
    """
    bw, bh = int(box[2]) - int(box[0]), int(box[3]) - int(box[1])
    k = long_side / max(bw, bh)
    w = max(16, int(round(bw * k / 16)) * 16)
    h = max(16, int(round(bh * k / 16)) * 16)
    return w, h


def _gen_alive(base):
    """Ждать живого приложения фермы (system_stats отвечает) и вернуть ГиБ свободно."""
    deadline = time.monotonic() + COMFY_GEN_FREE_WAIT
    while True:
        try:
            r = httpx.get(f'{base}/system_stats', timeout=10)
            if r.is_success:
                return r.json()['system']['ram_free'] / 2**30
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f'ферма {base} не ожила за {COMFY_GEN_FREE_WAIT:.0f} с')
        time.sleep(2)


def _wf_fill(node, values):
    """Заменить строковые значения-маркеры (и подстроки внутри) по всему JSON."""

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            for m in COMFY_GEN_MARKERS:
                if m in x:
                    return values[m] if x == m else x.replace(m, str(values[m]))
        return x
    return walk(node)


def _wf_set(node, path, value):
    """Точечно: узел.вход = значение, path вида 'node_id.вход' (для workflow
    без маркеров, где вход надо просто переписать)."""
    nid, field = path.rsplit('.', 1)
    node[nid]['inputs'][field] = value


def _run_one(wf, scene, out, base, seed, need=0.0, timeout=COMFY_GEN_TIMEOUT,
             farm_ssh='', farm_container='', remote=COMFY_GEN_REMOTE):
    """Один запуск /prompt -> /history -> /view; кадры на диск, строки манифеста.

    need>0 — ГБ, которые задача приблизительно съест: память уже выгружена
    (`comfy_gen_free` выше по стеку) и этого всё равно не влезает вместе с
    запасом — отказ, не отправляя (единая память GB10, переполнение валит всю
    ферму). Приложение фермы перезапускается посреди
    прогона и стирает свой queue/history — граф досылается заново (_gen_await),
    прогон доходит до конца, а не падает с Connection refused."""
    if need:
        free = httpx.get(f'{base}/system_stats', timeout=30).json()['system']['ram_free'] / 2**30
        if free < need + COMFY_GEN_MEM_MARGIN:
            raise RuntimeError(f'на ферме свободно {free:.0f} ГБ, задаче надо '
                               f'≥{need + COMFY_GEN_MEM_MARGIN:.0f} — не отправляю')
    for n in wf.values():  # API-форма: каждый узел с class_type
        n.setdefault('class_type', n.pop('_cls'))
    done = _gen_await({'prompt': wf, 'client_id': str(uuid.uuid4())}, base,
                      time.monotonic() + timeout, timeout)
    rows = []
    for o in done.get('outputs', {}).values():
        for img in o.get('images', []):
            b = httpx.get(f'{base}/view', params={k: img[k] for k in
                             ('filename', 'subfolder', 'type')}, timeout=60)
            b.raise_for_status()
            rows.append(_save(b.content, Path(img['filename']).suffix or '.png',
                              out, scene, seed))
            if farm_ssh and img.get('type') == 'output':
                # кадр уже в проекте — на ферме он больше не лежит
                _farm_sh(farm_ssh, farm_container, 'rm -f '
                         + str(Path(remote) / img['subfolder'] / img['filename']))
    return rows


class _GenLost(RuntimeError):
    """Приложение фермы перезапустилось и стёрло граф из своей памяти; досылаем."""


def _gen_await(body, base, deadline, timeout):
    """Шлёт body на /prompt и ждёт pid в /history; connection-разрывы и рестарты
    приложения переживает: в щель крутимся, а если граф пропал с живого фермы
    (queue чист, в history нет) — досылаем заново, пока не упрёмся в дедлайн."""
    while True:
        try:
            r = httpx.post(f'{base}/prompt', json=body, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f'/prompt {r.status_code}: {r.text[:500]}')
            pid = r.json()['prompt_id']
            while True:
                h = httpx.get(f'{base}/history/{pid}', timeout=30).json()
                if pid in h:
                    entry = h[pid]
                    st = entry.get('status', {})
                    if st.get('status_str') != 'success':
                        msgs = st.get('messages', [])
                        if any(m[0] == 'execution_interrupted' and
                               not m[1].get('exception_message') for m in msgs):
                            # сняли с очереди чужой рукой, без нашей ошибки — дослать
                            raise _GenLost(f'ферма прервала прогон {pid[:8]}')
                        raise RuntimeError(f'workflow {pid}: '
                                           f'{json.dumps(st, ensure_ascii=False)[:400]}')
                    return entry
                q = httpx.get(f'{base}/queue', timeout=30).json()
                if pid not in [it[1] for it in q['queue_running'] + q['queue_pending']]:
                    raise _GenLost(f'граф пропал с фермы (pid {pid[:8]})')
                if time.monotonic() > deadline:
                    raise TimeoutError(f'ComfyUI не закончил {pid} за {timeout} c')
                time.sleep(2)
        except (_GenLost, httpx.TransportError) as err:
            if time.monotonic() > deadline:
                raise TimeoutError(f'за {timeout} с ферма не сделала задачу: {err}') from err
            time.sleep(5)  # приложение фермы в рестарте — подождать и дослать


def _save(data, ext, out, scene, seed):
    """EXIF долой, имя — хэш содержимого, расширение как у файла фермы."""
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ('_gen_raw' + ext)
    tmp.write_bytes(data)
    subprocess.run(['exiftool', '-all=', '-overwrite_original', str(tmp)],
                   check=True, capture_output=True)
    name = hashlib.sha256(tmp.read_bytes()).hexdigest()[:32] + ext
    tmp.rename(d / name)
    row = {'file': name, 'scene': scene['id'], 'seed': seed,
           'nsfw': scene['nsfw'], 'face_on': scene.get('face_on', True),
           'score': None, 'pass': None}
    mp = d / 'manifest.json'
    man = json.loads(mp.read_text()) if mp.exists() else []
    man.append(row)
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=1) + '\n')
    return row


def _comfy_gen_qa(rows, out, anchor, det=COMFY_GEN_QA_DET,
                  qa_pass=COMFY_GEN_QA_PASS):
    """Скорить кадры с лицом против якоря: score/pass — в строки и в манифест.

    Кроит лицо кропом и скорит тем же det, каким кроил — та же цепочка, чем
    мерили сходство вручную, иначе драйвер и человек видят разные числа.
    """
    import tempfile
    from ai.ai_face import ai_face_crop, ai_face_score
    by_file = {}
    mp = Path(out) / 'manifest.json'
    if mp.exists():
        by_file = {r['file']: r for r in json.loads(mp.read_text())}
    with tempfile.TemporaryDirectory() as td:
        for r in rows:
            crop = Path(td) / (Path(r['file']).stem + '.png')
            try:
                ai_face_crop(Path(out) / r['file'], crop, det_size=tuple(det))
                s = ai_face_score(crop, anchor, det_size=tuple(det))
            except (ValueError, OSError):
                s = 0.0  # лицо не нашлось — это провал кадра, не авария батча
            ok = s >= qa_pass
            r['score'], r['pass'] = s, ok
            if r['file'] in by_file:
                by_file[r['file']].update({'score': s, 'pass': ok})
    if by_file:
        mp.write_text(json.dumps(list(by_file.values()), ensure_ascii=False,
                                  indent=1) + '\n')


def _farm_sh(host, container, sh):
    """Одна команда внутри контейнера фермы (ls/cat/rm готовых файлов); stdout."""
    return subprocess.run(['ssh', host, f'docker exec {container} sh -c "{sh}"'],
                          capture_output=True, text=True, check=True).stdout


def _farm_pull(host, container, remote, rel, out, name):
    """Готовый файл фермы — в проект и стереть там: ферма не склад готового."""
    p = Path(out) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(subprocess.run(
        ['ssh', host, f'docker exec {container} cat {Path(remote) / rel}'],
        capture_output=True, check=True).stdout)
    _farm_sh(host, container, f'rm -f {Path(remote) / rel}')
    return p


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Прогонить workflow персоны над сценой (или всеми сценами).')
    parser.add_argument('command', choices=['free', 'model', 'run', 'train', 'swap'],
                        help='free — выгрузить кэш моделей фермы; model — залить '
                             'лору-актив в models фермы и рестартнуть её; run — '
                             'гонять сцены; train — обучить лору на файлах; '
                             'swap — натянуть лицо лоры на готовые фото')
    parser.add_argument('--workflow', default='', help='API-JSON workflow')
    parser.add_argument('--scenes', default='',
                        help='scenes.json каталога персоны (список сцен)')
    parser.add_argument('--scene', default='', help='id одной сцены; пусто — все')
    parser.add_argument('--persona', default='', help='префикс промпта (имя персоны)')
    parser.add_argument('--files', nargs='*', default=[],
                        help='кадры датасета для train; лора-актив для model; '
                             'фото для swap')
    parser.add_argument('--mode', default='face',
                        choices=('face', 'face_hair', 'head_neck'),
                        help='swap: насколько широка выкройка (только лицо / '
                             'с причёской / голова с шеей)')
    parser.add_argument('--anchor', default='', help='файл якоря, если workflow ждёт')
    parser.add_argument('--face', default='',
                        help='swap: лицо персоны — файл или каталог патчей (для '
                             'каталога инструмент сам берёт самый чистый под '
                             'ракурс кадра); против него скорится готовая '
                             'заплата; пусто — против выкроенного лица оригинала')
    parser.add_argument('--anchor-input', default='',
                        help='«узел.вход» для workflow без маркера __ANCHOR__')
    parser.add_argument('--qa-anchor', default='',
                        help='лицо-якорь для score/pass; провал — тот же прогон на seed+1000')
    parser.add_argument('--qa-det', default='',
                        help=f'детекция «W,H» для score/pass (пусто — '
                             f'{COMFY_GEN_QA_DET[0]},{COMFY_GEN_QA_DET[1]})')
    parser.add_argument('--base', default='http://127.0.0.1:8188', help='адрес ComfyUI')
    parser.add_argument('--out', default='', help='каталог персоны под кадры (run)')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--n', type=int, default=1, help='кадров на сцену')
    parser.add_argument('--denoise', type=float, default=1.0,
                        help='сила изменения кадра-основы: для swap — стадия лица '
                             '(первый FaceDetailer в графе)')
    parser.add_argument('--detail', type=float, default=0.22,
                        help='swap: сила второй стадии FaceDetailer (текстура), '
                             'не меняющая геометрию')
    parser.add_argument('--hf', type=int, default=0,
                        help='swap: сколько самых мелких уровней пирамиды берут '
                             'текстуру кадра (0 — заплатка на всех уровнях)')
    parser.add_argument('--render', type=int, default=COMFY_GEN_SWAP_RENDER,
                        help='swap: длинная сторона рендера выкройки (её '
                             'пропорции сохраняются, размер кратен 16)')
    parser.add_argument('--steps', type=int, default=20, help='swap: шагов сэмплера')
    parser.add_argument('--lora', type=float, default=1.0,
                        help='swap: сила лоры лица в графе')
    parser.add_argument('--grain', type=float, default=1.0,
                        help='swap: множитель зерна (1.0 — ровно до кожи кадра, '
                             '0 — не класть)')
    parser.add_argument('--feather', type=float, default=0.0,
                        help='swap: перо маски в ПИКСЕЛЯХ (0 — от высоты лица)')
    parser.add_argument('--no-parts', action='store_true',
                        help='swap: не спрашивать vision, что в выкройке кроме лица')
    parser.add_argument('--fit', type=float, default=0.0,
                        help='swap: сколько ОСТАВШЕГОСЯ рассогласования тона '
                             'доправить сверх гейта (0 — только избыток, '
                             '1 — полный сдвиг к коже кадра)')
    parser.add_argument('--reuse', action='store_true',
                        help='не выгружать модели фермы, если памяти и так '
                             'хватает: для пачки кадров по одному графу это '
                             'экономит перезагрузку 17 ГБ на каждый кадр')
    parser.add_argument('--qa-pass', type=float, default=COMFY_GEN_QA_PASS,
                        help='порог косинуса приёмки; 0 — не пересевать прогон '
                             '(на лице в сотню пикселей общий 0.5 недостижим)')
    parser.add_argument('--budget', default='512x512',
                        help='train: бюджет площади «ВхН»: патч сверх него жался бы пропорционально, '
                             'внутри — родной размер, кратно 16')
    parser.add_argument('--farm-ssh', default='',
                        help='ssh-хост фермы; с ним готовые файлы (лора, кадры) снимаются с фермы в --out и стираются там')
    parser.add_argument('--farm-container', default='',
                        help='именование контейнера ComfyUI на ферме')
    ns = parser.parse_args()
    try:
        if ns.command == 'model':
            for name in comfy_gen_model(ns.files, ns.base, ns.farm_ssh,
                                        ns.farm_container):
                print(name)
            print('на ферме')
            raise SystemExit
        if ns.command == 'free':
            print(f"свободно {comfy_gen_free(ns.base, ns.farm_ssh,
                                             ns.farm_container):.1f} ГиБ")
            raise SystemExit
        if ns.command == 'train':
            w, h = (int(v) for v in ns.budget.lower().split('x'))
            got = comfy_gen_train(ns.workflow, ns.files, base=ns.base,
                                  caption=ns.persona, seed=ns.seed, budget=(w, h),
                                  steps=ns.steps, out=ns.out, farm_ssh=ns.farm_ssh,
                                  farm_container=ns.farm_container)
            print(*(str(p) for p in got) or ['обучено'])
            if got:
                print('обучено')
            raise SystemExit
        if ns.command == 'swap':
            if not ns.out:
                raise SystemExit('ошибка: swap требует --out (каталог под фото)')
            det = (tuple(int(v) for v in ns.qa_det.split(','))
                   if ns.qa_det else COMFY_GEN_QA_DET)
            got = comfy_gen_swap(ns.files, ns.workflow, ns.out, base=ns.base,
                                 persona=ns.persona, mode=ns.mode, face=ns.face,
                                 denoise=ns.denoise, detail=ns.detail,
                                 hf=ns.hf, render=ns.render, steps=ns.steps,
                                 lora=ns.lora, grain=ns.grain,
                                 feather=ns.feather, parts_ask=not ns.no_parts,
                                 qa_pass=ns.qa_pass, fit_full=ns.fit,
                                 reuse=ns.reuse,
                                 seed=ns.seed, qa_det=det,
                                 farm_ssh=ns.farm_ssh,
                                 farm_container=ns.farm_container)
            for r in got:
                print(r['file'], r['scene'], r['seed'], r['score'], r['pass'],
                      'зерно', r['grain']['now'], 'маска', r['mask']['cover'])
            raise SystemExit
        if not ns.out:
            raise SystemExit('ошибка: run требует --out (каталог под кадры)')
        scenes = json.loads(Path(ns.scenes).read_text())
        if ns.scene:
            scenes = [s for s in scenes if s['id'] == ns.scene]
        qa_det = (tuple(int(v) for v in ns.qa_det.split(','))
                  if ns.qa_det else COMFY_GEN_QA_DET)
        rows = []
        for s in scenes:
            rows.extend(comfy_gen_batch(ns.workflow, s, ns.out, base=ns.base,
                                        persona=ns.persona, anchor=ns.anchor or None,
                                        anchor_input=ns.anchor_input,
                                        seed=ns.seed, n=ns.n, denoise=ns.denoise,
                                        qa_anchor=ns.qa_anchor, qa_det=qa_det,
                                        farm_ssh=ns.farm_ssh,
                                        farm_container=ns.farm_container))
        for r in rows:
            print(r['file'], r['scene'], r['seed'], r['score'], r['pass'])
    except (ValueError, KeyError, OSError, RuntimeError,
            httpx.HTTPError) as err:
        raise SystemExit(f'ошибка: {err}')

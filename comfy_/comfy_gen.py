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
Поэтому workflow несёт верхним ключом `_mem` — примерный аппетит в ГБ; драйвер
не пошлёт /prompt, если на ферме свободно меньше `_mem` + запас.

Кадры сходят с конвейера без EXIF (exiftool -all=) и с именами-хэшами, как
файлы на сайте: sha256[:32].jpg. Каждая минута провала — seed+1000, это решает
вызвавший, драйвер лишь честно исполняет прогон и пишет score/pass в манифест
построчно — их потом правит ai_face(check).
"""
import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

# Прогон тяжёлого workflow (flux + лоры) на ферме — минуты; ждём без стеснения.
COMFY_GEN_TIMEOUT = 900.0

# Маркеры в API-JSON workflow: значения узлов, которые драйвер подставляет.
COMFY_GEN_MARKERS = ('__PROMPT__', '__ANCHOR__', '__SEED__', '__DENOISE__')

# Запас к `_mem` workflow: чужие процессы (рабочая чат-модель) уже учтены в
# ram_free фермы — запас лишь на скачок живого остатка, не на соседей. Запас
# — фактический зазор, оставленный прогоном: у обучения на 15 заплатках он был
# 0.4 ГиБ (free 93.6→0.4) и прогон жил; 4 ГиБ «на глаз» тут не оставляют места
# никакой задаче (свободно максимум 93.7).
COMFY_GEN_MEM_MARGIN = 0.3

# Обучение — те же шаги × градиент-аккумуляция прогонов, оно медленнее инференса:
# ждём час, а не 900 с (проба на заплатке не дошла до SaveLoRA именно на 900 с).
COMFY_GEN_TRAIN_TIMEOUT = 3600.0
COMFY_GEN_TRAIN_SIZE = (384, 384)  # живые заплатки лиц 344×461…987×1342; flux1-dev-fp8
                                   # на 768×1024 (0.79 МП/кадр) не влезает в память и падает
                                   # torch.OutOfMemoryError при free 93.5 — см. docs/comfy_gen.md
COMFY_GEN_REMOTE = '/opt/ComfyUI/output'  # каталог готовых файлов внутри контейнера фермы

# QA кадра с лицом: косинус против якоря не ниже порога — иначе прогон на seed+сдвиг.
COMFY_GEN_QA_PASS = 0.5
COMFY_GEN_QA_RETRY_SEED = 1000


def comfy_gen_batch(workflow, scene, out, *, base, persona='', anchor=None,
                    anchor_input='', seed=1, n=1, denoise=1.0, qa_anchor='',
                    farm_ssh='', farm_container='', remote=COMFY_GEN_REMOTE):
    """Прогнать workflow над сценой n раз; вернуть строки манифеста по кадрам.

    Кадры с лицом (scene.face_on) скорятся лицом-якорем `qa_anchor`: по порогу
    `COMFY_GEN_QA_PASS` кадр проходит, иначе тот же прогон на seed+COMFY_GEN_QA_RETRY_SEED.

    workflow — путь к API-JSON; scene — запись scenes.json (id/prompt/nsfw/
    face_on); out — каталог куда класть кадры; base — адрес ComfyUI
    («http://хост:порт»); anchor — файл якорного лица, грузится в input фермы,
    его новое имя подставляется вместо __ANCHOR__ (workflow без якоря его
    просто не содержит); seed — стартовый, i-й кадр — seed+i; `_mem` workflow
    (ГБ) сверяется со свободной памятью фермы до отправки — не влезает — отказ.
    """
    wf = json.loads(Path(workflow).read_text())
    need = float(wf.pop('_mem', 0))
    anchor_name = comfy_gen_upload(anchor, base) if anchor else ''
    if anchor_input:
        _wf_set(wf, anchor_input, anchor_name or str(anchor))
    rows = []
    for i in range(n):
        body = lambda s: _wf_fill(json.loads(json.dumps(wf)),
                                  {'__PROMPT__': f'{persona}, {scene["prompt"]}'.strip(', '),
                                   '__ANCHOR__': anchor_name,
                                   '__SEED__': str(s),
                                   '__DENOISE__': str(denoise)})
        got = _run_one(body(seed + i), scene, out, base, seed + i, need,
                       farm_ssh=farm_ssh, farm_container=farm_container)
        if qa_anchor and scene.get('face_on', True):
            _comfy_gen_qa(got, out, qa_anchor)
            if not all(r['pass'] for r in got):
                got = _run_one(body(seed + i + COMFY_GEN_QA_RETRY_SEED), scene, out,
                               base, seed + i + COMFY_GEN_QA_RETRY_SEED, need,
                               farm_ssh=farm_ssh, farm_container=farm_container)
                _comfy_gen_qa(got, out, qa_anchor)
        rows.extend(got)
    return rows


def comfy_gen_train(workflow, files, *, base, caption, seed=1, size=COMFY_GEN_TRAIN_SIZE,
                    out='', farm_ssh='', farm_container='', remote=COMFY_GEN_REMOTE):
    """Обучить LoRA на пачке кадров; вернуть локальные пути готовых лор.

    workflow — API-JSON с TrainLoraNode/SaveLoRA; files — локальные кадры
    датасета: каждый грузится в input фермы, ланцошем приводится к `size`
    (LatentBatch складывает только одинаковые латенты — заплатки лиц разного
    размера; родной размер заплатки диктует size, не 512² вслепую) и вшивается
    цепочкой LoadImage→ImageScale→VAEEncode→LatentBatch в узел TrainLoraNode;
    caption — единая подпись датасета (id персоны, не сцены).

    Лора — актив проекта, ферма её не хранит: при заданных farm_ssh/
    farm_container свежий файл снимается с фермы в out (имя — из prefix
    SaveLoRA без счётчика шагов) и стирается на ферме; без farm_* глухо
    остаётся на ферме (зачем актив чужой установки лежит здесь).
    """
    wf = json.loads(Path(workflow).read_text())
    need = float(wf.pop('_mem', 0))
    enc = None
    for k, f in enumerate(files):
        lid, rid, eid = str(101 + k), str(201 + k), str(401 + k)
        wf[lid] = {'_cls': 'LoadImage', 'inputs': {'image': comfy_gen_upload(f, base)}}
        wf[rid] = {'_cls': 'ImageScale', 'inputs': {'image': [lid, 0],
                    'upscale_method': 'lanczos', 'crop': 'disabled',
                    'width': size[0], 'height': size[1]}}
        wf[eid] = {'_cls': 'VAEEncode', 'inputs': {'pixels': [rid, 0], 'vae': ['3', 0]}}
        if enc is None:
            enc = [eid, 0]
        else:
            wf[f'3{k:02d}'] = {'_cls': 'LatentBatch',
                               'inputs': {'samples1': enc, 'samples2': [eid, 0]}}
            enc = [f'3{k:02d}', 0]
    for n in wf.values():
        if n['_cls'] == 'TrainLoraNode':
            n['inputs']['latents'] = enc
    run = _wf_fill(json.loads(json.dumps(wf)),
                   {'__PROMPT__': caption, '__ANCHOR__': '', '__SEED__': str(seed),
                    '__DENOISE__': '1.0'})
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
    return got


def comfy_gen_upload(path, base):
    """Залить файл в input фермы через /upload/image; вернуть имя на ферме."""
    p = Path(path)
    r = httpx.post(f'{base}/upload/image',
                   files={'image': (p.name, p.read_bytes())}, timeout=60)
    r.raise_for_status()
    name = r.json()['name']
    return name


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

    need>0 — ГБ, которые задача приблизительно съест: не влезает в свободную
    память фермы вместе с запасом — отказ, не отправляя (единая память GB10,
    переполнение валит всю ферму). Приложение фермы перезапускается посреди
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
                    if entry.get('status', {}).get('status_str') != 'success':
                        raise RuntimeError(f'workflow {pid}: '
                                           f'{json.dumps(entry.get("status"), ensure_ascii=False)[:400]}')
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


def _comfy_gen_qa(rows, out, anchor):
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
                ai_face_crop(Path(out) / r['file'], crop)
                s = ai_face_score(crop, anchor)
            except (ValueError, OSError):
                s = 0.0  # лицо не нашлось — это провал кадра, не авария батча
            ok = s >= COMFY_GEN_QA_PASS
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
    parser.add_argument('command', choices=['run', 'train'],
                        help='run — гонять сцены; train — обучить лору на файлах')
    parser.add_argument('--workflow', required=True, help='API-JSON workflow')
    parser.add_argument('--scenes', default='',
                        help='scenes.json каталога персоны (список сцен)')
    parser.add_argument('--scene', default='', help='id одной сцены; пусто — все')
    parser.add_argument('--persona', default='', help='префикс промпта (имя персоны)')
    parser.add_argument('--files', nargs='*', default=[],
                        help='кадры датасета для train')
    parser.add_argument('--anchor', default='', help='файл якоря, если workflow ждёт')
    parser.add_argument('--anchor-input', default='',
                        help='«узел.вход» для workflow без маркера __ANCHOR__')
    parser.add_argument('--qa-anchor', default='',
                        help='лицо-якорь для score/pass; провал — тот же прогон на seed+1000')
    parser.add_argument('--base', default='http://127.0.0.1:8188', help='адрес ComfyUI')
    parser.add_argument('--out', default='', help='каталог персоны под кадры (run)')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--n', type=int, default=1, help='кадров на сцену')
    parser.add_argument('--denoise', type=float, default=1.0,
                        help='сила изменения кадра-основы (img2img)')
    parser.add_argument('--size', default='384x384',
                        help='train: «ВхН» датасета под LatentBatch (по умолчанию родной размер заплаток)')
    parser.add_argument('--farm-ssh', default='',
                        help='ssh-хост фермы; с ним готовые файлы (лора, кадры) снимаются с фермы в --out и стираются там')
    parser.add_argument('--farm-container', default='',
                        help='именование контейнера ComfyUI на ферме')
    ns = parser.parse_args()
    try:
        if ns.command == 'train':
            w, h = (int(v) for v in ns.size.lower().split('x'))
            got = comfy_gen_train(ns.workflow, ns.files, base=ns.base,
                                  caption=ns.persona, seed=ns.seed, size=(w, h),
                                  out=ns.out, farm_ssh=ns.farm_ssh,
                                  farm_container=ns.farm_container)
            print(*(str(p) for p in got) or ['обучено'])
            if got:
                print('обучено')
            raise SystemExit
        if not ns.out:
            raise SystemExit('ошибка: run требует --out (каталог под кадры)')
        scenes = json.loads(Path(ns.scenes).read_text())
        if ns.scene:
            scenes = [s for s in scenes if s['id'] == ns.scene]
        rows = []
        for s in scenes:
            rows.extend(comfy_gen_batch(ns.workflow, s, ns.out, base=ns.base,
                                        persona=ns.persona, anchor=ns.anchor or None,
                                        anchor_input=ns.anchor_input,
                                        seed=ns.seed, n=ns.n, denoise=ns.denoise,
                                        qa_anchor=ns.qa_anchor,
                                        farm_ssh=ns.farm_ssh,
                                        farm_container=ns.farm_container))
        for r in rows:
            print(r['file'], r['scene'], r['seed'], r['score'], r['pass'])
    except (ValueError, KeyError, OSError, RuntimeError,
            httpx.HTTPError) as err:
        raise SystemExit(f'ошибка: {err}')

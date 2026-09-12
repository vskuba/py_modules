"""
Пачка кадров персоны: сцена JSON -> ComfyUI-API -> файл + строка манифеста.

Драйвер ничего не знает про девушек и сцены: он исполняет workflow в API-форме
(тот лежит у персоны рядом с scenes.json), подставляя маркеры — __PROMPT__
(текст сцены), __ANCHOR__ (имя загруженного якоря), __SEED__ (номер запуска).
Откуда берётся консистентность лица — PuLID в workflow или обученный LoRA, —
дело workflow, не драйвера.

Якорь приходит в ферму POST /upload/image — драйвер не знает ни хостов, ни
ssh: тот же код гоняет против любой ComfyUI по http.

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
COMFY_GEN_MARKERS = ('__PROMPT__', '__ANCHOR__', '__SEED__')


def comfy_gen_batch(workflow, scene, out, *, base, persona='', anchor=None,
                    anchor_input='', seed=1, n=1):
    """Прогнать workflow над сценой n раз; вернуть строки манифеста по кадрам.

    workflow — путь к API-JSON; scene — запись scenes.json (id/prompt/nsfw/
    face_on); out — каталог куда класть кадры; base — адрес ComfyUI
    («http://хост:порт»); anchor — файл якорного лица, грузится в input фермы,
    его новое имя подставляется вместо __ANCHOR__ (workflow без якоря его
    просто не содержит); seed — стартовый, i-й кадр — seed+i.
    """
    wf = json.loads(Path(workflow).read_text())
    anchor_name = comfy_gen_upload(anchor, base) if anchor else ''
    if anchor_input:
        _wf_set(wf, anchor_input, anchor_name or str(anchor))
    rows = []
    for i in range(n):
        run = _wf_fill(json.loads(json.dumps(wf)),
                       {'__PROMPT__': f'{persona}, {scene["prompt"]}'.strip(', '),
                        '__ANCHOR__': anchor_name,
                        '__SEED__': str(seed + i)})
        rows.extend(_run_one(run, scene, out, base, seed + i))
    return rows


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


def _run_one(wf, scene, out, base, seed):
    """Один запуск /prompt -> /history -> /view; кадры на диск, строки манифеста."""
    pid = str(uuid.uuid4())
    for n in wf.values():  # API-форма: каждый узел с class_type
        n.setdefault('class_type', n.pop('_cls'))
    r = httpx.post(f'{base}/prompt', json={'prompt': wf, 'client_id': pid},
                   timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f'/prompt {r.status_code}: {r.text[:500]}')
    prompt_id = r.json()['prompt_id']
    deadline = time.monotonic() + COMFY_GEN_TIMEOUT
    while True:
        h = httpx.get(f'{base}/history/{prompt_id}', timeout=30).json()
        if prompt_id in h:
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f'ComfyUI не закончил {prompt_id} за {COMFY_GEN_TIMEOUT} c')
        time.sleep(2)
    done = h[prompt_id]
    if done.get('status', {}).get('status_str') != 'success':
        raise RuntimeError(f'workflow {prompt_id}: {json.dumps(done.get("status"), ensure_ascii=False)[:400]}')
    rows = []
    for o in done.get('outputs', {}).values():
        for img in o.get('images', []):
            b = httpx.get(f'{base}/view', params={k: img[k] for k in
                             ('filename', 'subfolder', 'type')}, timeout=60)
            b.raise_for_status()
            rows.append(_save(b.content, Path(img['filename']).suffix or '.png',
                              out, scene, seed))
    return rows


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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Прогонить workflow персоны над сценой (или всеми сценами).')
    parser.add_argument('command', choices=['run'], help='run — гонять сцены')
    parser.add_argument('--workflow', required=True, help='API-JSON workflow')
    parser.add_argument('--scenes', required=True,
                        help='scenes.json каталога персоны (список сцен)')
    parser.add_argument('--scene', default='', help='id одной сцены; пусто — все')
    parser.add_argument('--persona', default='', help='префикс промпта (имя персоны)')
    parser.add_argument('--anchor', default='', help='файл якоря, если workflow ждёт')
    parser.add_argument('--anchor-input', default='',
                        help='«узел.вход» для workflow без маркера __ANCHOR__')
    parser.add_argument('--base', default='http://127.0.0.1:8188', help='адрес ComfyUI')
    parser.add_argument('--out', required=True, help='каталог персоны под кадры')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--n', type=int, default=1, help='кадров на сцену')
    ns = parser.parse_args()
    try:
        scenes = json.loads(Path(ns.scenes).read_text())
        if ns.scene:
            scenes = [s for s in scenes if s['id'] == ns.scene]
        rows = []
        for s in scenes:
            rows.extend(comfy_gen_batch(ns.workflow, s, ns.out, base=ns.base,
                                        persona=ns.persona, anchor=ns.anchor or None,
                                        anchor_input=ns.anchor_input,
                                        seed=ns.seed, n=ns.n))
        for r in rows:
            print(r['file'], r['scene'], r['seed'])
    except (ValueError, KeyError, OSError, RuntimeError) as err:
        raise SystemExit(f'ошибка: {err}')

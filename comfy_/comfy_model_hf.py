"""Что файл веса просит с HF: размер и sha256 из API репо — до скачки.

`comfy_model_get` сверяет скачанное с ожиданием, но ожидание два вечера кряду
приходило из curl: tree-API репо, `lfs.oid`, ещё и «Invalid username or
password» с чужой машины — и качали вслепую, а огрызок с доехавшим хвостом
распознавался только постфактум. Здесь та пара чисел — {'bytes', 'sha256'} —
одним вызовом, ровно в той форме, в какой их ждёт `comfy_model_get`.

Знания о конкретном проекте тут нет: репо и путь к файлу приходят вызывающему.
"""
import json
import urllib.error
import urllib.request

HF_API = 'https://huggingface.co/api'


def comfy_model_hf_expect(repo: str, file: str, *, ref: str = 'main',
                          token: str = '') -> dict:
    """Ожидание файла из репо HF: url докачки, размер, sha256 (lfs oid).

    Args:
        repo: репозиторий HF (`Comfy-Org/stable-diffusion-v1-5-archive`, …).
        file: путь к файлу в репо (`sd_xl_base_1.0.safetensors`, …).
        ref: ветка/ревизия; пусто — main.
        token: Bearer для приватных/гейтнутых репо (пусто — без него).

    Returns:
        {'url': resolve-ссылка, 'bytes': int, 'sha256': str, 'why': str} —
        `sha256` пуст для мелких файлов вне lfs (у них хеша в API нет, размер
        есть); при провале API поля пустые и слово в 'why'.
    """
    url = f'{HF_API}/models/{repo}/tree/{ref}?recursive=true'
    req = urllib.request.Request(url)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            listing = json.load(r)
    except Exception as e:
        return {'url': '', 'bytes': 0, 'sha256': '', 'why': f'HF API молчит: {e}'}
    tail = file.rsplit('/', 1)[-1]
    entry = next((f for f in listing if f.get('path') == file), None) or next(
        (f for f in listing if f.get('path', '').rsplit('/', 1)[-1] == tail), None)
    if entry is None:
        return {'url': '', 'bytes': 0, 'sha256': '',
                'why': f'файла «{file}» в {repo}@{ref} нет'}
    return {'url': f'https://huggingface.co/{repo}/resolve/{ref}/'
            + urllib.request.quote(file),
            'bytes': entry.get('size', 0),
            'sha256': (entry.get('lfs') or {}).get('oid', ''),
            'why': '' if entry.get('lfs') else
            'файл вне lfs — sha в API нет, сверяй только размер'}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='что просит файл веса с HF: размер и sha256 до скачки — '
                    'в той форме, в какой их ждёт comfy_model_get.')
    ap.add_argument('repo', help='репо HF')
    ap.add_argument('file', help='путь к файлу в репо')
    ap.add_argument('--ref', default='main', help='ветка/ревизия')
    ap.add_argument('--token', default='', help='Bearer для гейтнутых репо')
    ns = ap.parse_args()
    print(json.dumps(comfy_model_hf_expect(ns.repo, ns.file, ref=ns.ref,
                                           token=ns.token), ensure_ascii=False))

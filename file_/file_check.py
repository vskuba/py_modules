"""Цел ли файл: не пустышка, не огрызок, голова сходится с расширением.

Разгром Марины начался с огрызка: трейнер оставил `marina_face.safetensors` в 0
байт, исполнитель проверил `is_file()` — и пустой файл объявился обученной лорой.
`is_file()` отвечает «файл есть», а не «файл целый»; здесь — второй вопрос одним
вызовом: непустой ли, магическая голова совпадает с расширением, вес и хеш — с
ожиданием.

Что именно ловится:
* пустышка (0 байт) — тот самый огрызок;
* огрызок поперёк: у `.safetensors` первые 8 байт — длина JSON-заголовка, и
  файл короче, чем обещает заголовок, — недокачан;
* подмена расширения: jpg с головой PNG — контент не тот, что думает система.
"""
import argparse
import hashlib
import struct

# магическая голова по расширению; нет в списке — сверяется только размер/хеш
FILE_CHECK_MAGIC = {'jpg': b'\xff\xd8\xff', 'jpeg': b'\xff\xd8\xff',
                    'png': b'\x89PNG', 'gif': b'GIF8', 'webp': b'RIFF'}


def file_check(path: str, size: int = 0, sha256: str = '') -> dict:
    """Цел ли файл: непустой, голова сходится с расширением, байты с ожидаемым.

    Args:
        path: путь к файлу.
        size: ожидаемый размер (0 — не сверять).
        sha256: ожидаемый хеш содержимого (пусто — не сверять).

    Returns:
        {'цел': bool, 'байт': int, 'почему': str}: 'почему' — первая находка
        ('нет файла', 'пустышка', 'огрызок: …', 'голова не …', 'размер …',
        'хеш не сходится'); пусто, когда файл цел.
    """
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        return {'intact': False, 'bytes': 0, 'why': 'нет файла'}
    data = p.read_bytes()
    n = len(data)
    ext = p.suffix.lower().lstrip('.')
    why = ''
    if n == 0:
        why = 'пустышка'
    elif ext == 'safetensors':
        head = struct.unpack('<Q', data[:8])[0] if n >= 8 else 0
        if n < 8 or not head or data[8:9] != b'{' or n < 8 + head:
            why = f'огрызок: заголовок ждёт {8 + head} байт, файл {n}'
    else:
        magic = FILE_CHECK_MAGIC.get(ext, b'')
        if magic and not data.startswith(magic):
            why = f'голова не {ext}'
        elif ext == 'webp' and data[8:12] != b'WEBP':
            why = 'голова не webp'
    if not why and size and n != size:
        why = f'размер {n} ≠ ожидаемый {size}'
    if not why and sha256 and hashlib.sha256(data).hexdigest() != sha256:
        why = 'хеш не сходится'
    return {'intact': not why, 'bytes': n, 'why': why}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Цел ли файл: пустышка, огрызок, подмена расширения, '
                    'размер и хеш против ожидаемых.')
    ap.add_argument('files', nargs='+', help='файлы (оболочка разворачивает glob)')
    ap.add_argument('--size', type=int, default=0, help='ожидаемый размер')
    ap.add_argument('--sha', default='', help='ожидаемый sha256')
    ns = ap.parse_args()
    for f in ns.files:
        r = file_check(f, ns.size, ns.sha)
        print(f'{"цел" if r["intact"] else "НЕ ЦЕЛ"} {f}: {r["bytes"]} байт '
              f'{r["why"]}'.rstrip())

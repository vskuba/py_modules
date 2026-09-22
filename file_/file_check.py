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
                    'png': b'\x89PNG', 'gif': b'GIF8', 'webp': b'RIFF',
                    'pdf': b'%PDF-', 'gz': b'\x1f\x8b', 'zip': b'PK\x03\x04',
                    'apk': b'PK\x03\x04'}

# Сколько байт головы читаем. Шестнадцати хватает всем: самой длинной магии
# (`RIFF` + 4 байта длины + `WEBP`) и боксу ISO-BMFF у mp4/mov, где тип стоит
# после четырёх байт длины и в таблицу магий поэтому не укладывается.
FILE_CHECK_HEAD = 16

# Кусок потокового чтения при сверке хеша: файл веса бывает под десяток
# гигабайт, и `read_bytes()` на нём кладёт процесс (замер: 420 МБ файла —
# 415 МБ RSS). Мегабайт — компромисс между числом сисколлов и памятью.
FILE_CHECK_CHUNK = 1 << 20


def file_check(path: str, size: int = 0, sha256: str = '') -> dict:
    """Цел ли файл: непустой, голова сходится с расширением, байты с ожидаемым.

    Args:
        path: путь к файлу.
        size: ожидаемый размер (0 — не сверять).
        sha256: ожидаемый хеш содержимого (пусто — не сверять).

    Returns:
        {'intact': bool, 'bytes': int, 'why': str}: `why` — первая находка
        ('нет файла', 'пустышка', 'огрызок: …', 'голова не …', 'размер …',
        'хеш не сходится'); пусто, когда файл цел.

    ⚠ Файл целиком в память **не читается**: размер берётся `stat`, голова —
    первыми 16 байтами, хеш считается потоком. Иначе проверка веса ломается об
    то же, обо что ломался бы его пользователь: 420-мегабайтный файл поднимал
    RSS процесса с 14 до 415 МБ, а лоры бывают и крупнее.

    ⚠ Хеш — единственное, что читает весь файл, и только когда `sha256` задан.
    """
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        return {'intact': False, 'bytes': 0, 'why': 'нет файла'}
    n = p.stat().st_size
    with open(p, 'rb') as fh:
        head = fh.read(FILE_CHECK_HEAD)
    ext = p.suffix.lower().lstrip('.')
    why = ''
    if n == 0:
        why = 'пустышка'
    elif ext == 'safetensors':
        length = struct.unpack('<Q', head[:8])[0] if n >= 8 else 0
        if n < 8 or not length or head[8:9] != b'{' or n < 8 + length:
            why = f'огрызок: заголовок ждёт {8 + length} байт, файл {n}'
    elif ext in ('mp4', 'mov'):
        # ISO-BMFF: 4 байта длины бокса, затем тип. Первым боксом почти всегда
        # `ftyp`; сверять сами 4 байта длины бессмысленно — они любые.
        if head[4:8] not in (b'ftyp', b'moov', b'mdat', b'free', b'skip'):
            why = f'голова не {ext}'
    else:
        magic = FILE_CHECK_MAGIC.get(ext, b'')
        if magic and not head.startswith(magic):
            why = f'голова не {ext}'
        elif ext == 'webp' and head[8:12] != b'WEBP':
            why = 'голова не webp'
    if not why and size and n != size:
        why = f'размер {n} ≠ ожидаемый {size}'
    if not why and sha256 and _sha256(p) != sha256:
        why = 'хеш не сходится'
    return {'intact': not why, 'bytes': n, 'why': why}


# ── детали реализации ──

def _sha256(path) -> str:
    """Хеш файла потоком: единственное место, читающее его целиком."""
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        while chunk := fh.read(FILE_CHECK_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Цел ли файл: пустышка, огрызок, подмена расширения, '
                    'размер и хеш против ожидаемых.')
    ap.add_argument('files', nargs='+', help='файлы (оболочка разворачивает glob)')
    ap.add_argument('--size', type=int, default=0, help='ожидаемый размер')
    ap.add_argument('--sha', default='', help='ожидаемый sha256')
    ap.add_argument('--json', action='store_true',
                    help='машинный вывод вместо человеческого')
    ns = ap.parse_args()
    # Код возврата — число НЕцелых: `file_check x && дальше` читается кодом,
    # а не разбором строки «НЕ ЦЕЛ» из вывода.
    broken = 0
    rows = []
    for f in ns.files:
        r = file_check(f, ns.size, ns.sha)
        broken += 0 if r['intact'] else 1
        rows.append({'path': f, **r})
        if not ns.json:
            print(f'{"цел" if r["intact"] else "НЕ ЦЕЛ"} {f}: {r["bytes"]} байт '
                  f'{r["why"]}'.rstrip())
    if ns.json:
        import json
        print(json.dumps(rows, ensure_ascii=False))
    # Потолок 125: код возврата процесса живёт в байте, и ровно 256 нецелых
    # файлов дали бы «успех». Точное число — в выводе, коду хватает «сколько-то».
    raise SystemExit(min(broken, 125))

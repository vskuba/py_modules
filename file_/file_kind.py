"""Настоящий формат файла по первым байтам: расширение может врать, сигнатура — нет.

Кадр прогона лежит в `data/.../shot/…/1525.png`, а внутри JPEG: `read_image`
такое отвергает, PIL требует верного имени, а проверяющий глаз видит «файл не
открывается» там, где картинка целая. Расширение — ярлык, его ставит тот, кто
писал файл, и оно устаревает после смены кодека; начало файла ошибиться не
может.

`file_check` отвечает на «цел ли файл» и сверяет голову с ожидаемым
расширением; этот модуль — обратный вопрос: «что это, если ярлык ничего не
стоит», и показывает верное имя. Таблица магий одна на двоих — `FILE_CHECK_MAGIC`.
"""
import argparse
import os

from file_.file_check import FILE_CHECK_MAGIC

# Реестр `file_check` перевёрнут «голова → формат»: там по расширению ищут
# голову, здесь голова называет формат. `apk` и `zip` — одна магия, формат тоже
# один; `webp` — RIFF, а RIFF бывает и WAV, поэтому из таблицы он убран и
# проверяется отдельно, знаком `WEBP` на девятом байте.
_EXT_BY_MAGIC: dict[bytes, str] = {}
for _ext, _magic in FILE_CHECK_MAGIC.items():
    if _ext != 'webp':
        _EXT_BY_MAGIC.setdefault(_magic, 'zip' if _ext == 'apk' else _ext)

# Форматов, которых нет у `file_check`, здесь три: их ярлык тоже надо уметь
# подобрать, а спрашивать о них нечего — голова молчит, только сигнатура.
_KINDS = tuple((magic, ext, ext) for magic, ext in _EXT_BY_MAGIC.items()) + (
    (b'BM', 'bmp', 'bmp'),
    (b'II*\x00', 'tif', 'tif'),
    (b'MM\x00*', 'tif', 'tif'),
)

_MIME = {'jpg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif',
         'webp': 'image/webp', 'pdf': 'application/pdf', 'gz': 'application/gzip',
         'zip': 'application/zip', 'bmp': 'image/bmp', 'tif': 'image/tiff',
         'mp4': 'video/mp4', 'txt': 'text/plain', 'html': 'text/html',
         'json': 'application/json'}


def file_kind(path: str) -> dict:
    """Что это за файл на самом деле и не врёт ли расширение.

    Args:
        path: файл на диске.

    Returns:
        {'path', 'kind', 'mime', 'ext', 'ok', 'suggest'}: `suggest` — путь с
        правильным расширением, если нынешнее врёт (пусто — не врёт). У текста
        расширение не правим: .txt/.md/.html читаются равно, спорить не о чем.

    Raises:
        FileNotFoundError: файла нет.
    """
    with open(path, 'rb') as fh:
        head = fh.read(64)
    ext = os.path.splitext(path)[1].lower()
    kind = _sniff(head)
    if kind in ('txt', 'html', 'json'):
        return {'path': path, 'kind': kind, 'mime': _MIME[kind], 'ext': ext,
                'ok': True, 'suggest': ''}
    if not kind:
        return {'path': path, 'kind': 'binary', 'mime': 'application/octet-stream',
                'ext': ext, 'ok': True, 'suggest': ''}
    right = '.' + kind
    ok = ext == right
    return {'path': path, 'kind': kind, 'mime': _MIME[kind], 'ext': ext, 'ok': ok,
            'suggest': '' if ok else (path[:-len(ext)] if ext else path) + right}


def _sniff(head: bytes) -> str:
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return 'webp'
    if head[4:8] == b'ftyp':
        return 'mp4'
    for magic, kind, _ in _KINDS:
        if head.startswith(magic):
            return kind
    body = head[3:] if head.startswith(b'\xef\xbb\xbf') else head
    probe = body.lstrip()
    if probe.startswith((b'<', b'{', b'[')) or _looks_text(head):
        if probe.startswith(b'{') or probe.startswith(b'['):
            return 'json'
        if probe[:5].lower() in (b'<!doc', b'<html', b'<?xml'):
            return 'html'
        if probe.startswith(b'<'):
            return 'txt'          # svg и прочая разметка — на текстовую долю
        return 'txt'
    return ''


def _looks_text(head: bytes) -> bool:
    first = head.split(b'\n', 1)[0]
    allowed = bytes(range(0x20, 0x7F)) + b'\t\r'
    return bool(first) and all(byte in allowed for byte in first[:48])


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Настоящий формат файла по сигнатуре; враньё расширения — полем ok.')
    ap.add_argument('paths', nargs='+')
    ns = ap.parse_args()
    broken = 0
    for one in ns.paths:
        got = file_kind(one)
        mark = '' if got['ok'] else f"   !=  клади {got['suggest']}"
        print(f"{one}: {got['kind']} ({got['mime']}){mark}")
        broken += not got['ok']
    raise SystemExit(1 if broken else 0)

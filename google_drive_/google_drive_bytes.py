"""Байты файла с Google Диска: уменьшенное превью или оригинал.

Отдельно от двери и обхода, потому что здесь единственное место, где из Диска
выходят не метаданные, а содержимое, — и единственное, где важна цена.

**Превью против оригинала — это не оттенок качества, а два порядка трафика.**
Замерено на живом снимке: оригинал `1768×2656` весит 8 534 073 байта, его же
превью шириной 400 — 139 158. Разница **61-кратная**. Сетка из сорока плиток на
превью стоит ~5 МБ, на оригиналах — ~340 МБ. Поэтому всё, что показывается
списком, обязано брать превью, а оригинал — только когда его действительно
открыли.

⚠ **Превью приватного файла нельзя отдать браузеру ссылкой.** `thumbnailLink`
требует наших полномочий и живёт часы; вставленный в `<img src>`, он у чужого
браузера ответит страницей входа. Значит превью проксируют: байты берёт сервер
и отдаёт их уже своей ручкой. Токен при этом остаётся внутри.

⚠ **Свежезалитый файл приходит без `thumbnailLink`.** Диск рисует превью не
сразу. Это не ошибка и не повод прятать файл: такая плитка — заглушка
«превью ещё не готово», и на следующем обходе она оживёт сама.

Тема целиком — `docs/google_drive.md`.
"""

import asyncio

import httpx

from google_drive_.google_drive_ import (GOOGLE_DRIVE_API, google_drive_token)
from http_.http_pool import http_pool_transport

# Размеры превью, которые имеет смысл просить. Диск нарежет любой, но разнобой
# значений рвёт кэш браузера и кэш самого Диска: одна и та же плитка, запрошенная
# как 399 и 400, — две разные картинки для обоих.
GOOGLE_DRIVE_THUMB_SIZES = (200, 400, 800, 1600)


async def google_drive_bytes(file: dict | str, width: int = 0,
                             key_path: str = '') -> tuple[bytes, str]:
    """Содержимое файла Диска: превью заданной ширины либо оригинал целиком.

    Args:
        file: запись файла из `google_drive_list`/`google_drive_walk` — либо
            просто его `id`, но тогда превью недоступно (ссылка на него есть
            только в записи).
        width: ширина превью из `GOOGLE_DRIVE_THUMB_SIZES`. **Ноль — оригинал**,
            во всю величину и за весь трафик.
        key_path: путь к ключу; пусто — из настроек.

    Returns:
        `(байты, тип содержимого)`. Тип берётся из ответа Диска, а не из имени
        файла: расширение врёт чаще, чем заголовок.

    Raises:
        RuntimeError: превью просят у записи без `thumbnailLink`, ширина не из
            списка, Диск отказал, либо вместо байтов пришла страница входа.

    ⚠ **Видео целиком через себя не гоняют.** У записи видео превью — первый
    кадр, и он здесь же; а сам файл это десятки мегабайт, которым правильная
    дорога — ссылка в просмотрщик Диска, а не наш канал.
    """
    if width and width not in GOOGLE_DRIVE_THUMB_SIZES:
        raise RuntimeError(f'ширина превью не из набора {GOOGLE_DRIVE_THUMB_SIZES}: {width}')

    token = await google_drive_token(key_path)
    client = httpx.AsyncClient(timeout=120, transport=http_pool_transport(),
                               follow_redirects=False)
    headers = {'Authorization': f'Bearer {token}'}

    if width:
        if not isinstance(file, dict) or not file.get('thumbnailLink'):
            raise RuntimeError('у записи нет thumbnailLink — превью ещё не готово')
        response = await client.get(_thumb_url(file['thumbnailLink'], width), headers=headers)
    else:
        file_id = file['id'] if isinstance(file, dict) else str(file)
        response = await client.get(f'{GOOGLE_DRIVE_API}/files/{file_id}', headers=headers,
                                    params={'alt': 'media', 'supportsAllDrives': 'true'})

    # Перенаправление на вход — это отказ, а не содержимое. Без явной проверки
    # вызывающий сохранил бы HTML формы входа под именем картинки и узнал бы об
    # этом, только открыв «битый» файл.
    if response.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError('Диск увёл на вход — доступа к файлу нет')
    if response.status_code != 200:
        raise RuntimeError(f'Диск не отдал файл ({response.status_code}): {response.text[:200]}')

    return response.content, response.headers.get('Content-Type', '')


def google_drive_bytes_wait(file: dict | str, width: int = 0,
                            key_path: str = '') -> tuple[bytes, str]:
    """Синхронный вход в `google_drive_bytes` — для скриптов, CLI и тестов."""
    return asyncio.run(google_drive_bytes(file, width, key_path))


def _thumb_url(link: str, width: int) -> str:
    """Подменить размер в ссылке превью.

    Диск отдаёт её с собственным суффиксом (`=s220`), и просить другой размер
    означает этот суффикс заменить, а не дописать второй: `=s220=s400` Диск
    не понимает и отвечает прежней картинкой.
    """
    return link.split('=s')[0] + f'=s{width}'


if __name__ == '__main__':
    import argparse
    from pathlib import Path

    from google_drive_.google_drive_walk import google_drive_find, google_drive_walk

    parser = argparse.ArgumentParser(description='Забрать файл с Google Диска.')
    parser.add_argument('folder', help='имя общей папки либо её id')
    parser.add_argument('name', help='имя файла внутри (часть имени достаточно)')
    parser.add_argument('--width', type=int, default=0,
                        help=f'ширина превью {GOOGLE_DRIVE_THUMB_SIZES}; 0 — оригинал')
    parser.add_argument('--out', default='', help='куда сохранить; пусто — только размер')
    parser.add_argument('--key', default='', help='путь к JSON-ключу; пусто — GOOGLE_DRIVE_KEY')
    args = parser.parse_args()

    async def _main() -> None:
        found = await google_drive_find(args.folder, key_path=args.key, folders_only=True)
        folder_id = found[0]['id'] if found else args.folder
        hit = [f for f in await google_drive_walk(folder_id, args.key) if args.name in f['name']]
        if not hit:
            raise SystemExit(f'ошибка: файла с «{args.name}» в папке нет')

        raw, kind = await google_drive_bytes(hit[0], args.width, args.key)
        print(f'{hit[0]["path"]}: {len(raw)} б, {kind}')
        if args.out:
            Path(args.out).write_bytes(raw)
            print(f'сохранено: {args.out}')

    try:
        asyncio.run(_main())
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

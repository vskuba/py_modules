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

# Превью **по номеру файла**, без сохранённой ссылки. Снято живьём 01.10.2026:
# `drive.google.com/thumbnail?id=…` уводит `302` сюда, а здесь ответ приходит
# сразу — `200 image/png`, точный размер (`=s400` даёт 266×400 у портрета
# 1768×2656). Без токена тот же адрес отвечает `302`, то есть приватность файла
# на месте.
#
# ⚠ Зачем это нужно отдельно от `thumbnailLink`: та ссылка живёт часы, хранить
# её негде, а спрашивать запись Диска ради каждой плитки — сорок лишних
# запросов на одно открытие галереи. По номеру — ноль лишних.
GOOGLE_DRIVE_THUMB_BY_ID = 'https://lh3.googleusercontent.com/d/{file_id}=s{size}'


async def google_drive_bytes(file: dict | str, width: int = 0,
                             key_path: str = '') -> tuple[bytes, str]:
    """Содержимое файла Диска: превью заданной ширины либо оригинал целиком.

    Args:
        file: запись файла из `google_drive_list`/`google_drive_walk` — либо
            просто его `id`: превью берётся и по номеру, без сохранённой
            ссылки (`GOOGLE_DRIVE_THUMB_BY_ID`).
        width: **длинная сторона** превью из `GOOGLE_DRIVE_THUMB_SIZES`.
            **Ноль — оригинал**, во всю величину и за весь трафик.
        key_path: путь к ключу; пусто — из настроек.

    Returns:
        `(байты, тип содержимого)`. Тип берётся из ответа Диска, а не из имени
        файла: расширение врёт чаще, чем заголовок.

    Raises:
        RuntimeError: ширина не из списка, Диск отказал, либо вместо байтов
            пришла страница входа.

    ⚠ `width` ограничивает **длинную** сторону, а не ширину: у портрета
    1768×2656 размер 400 даёт 266×400. Это суффикс `=s` Диска; суффикс `=w`
    задавал бы ширину и вернул бы 400×601 — вдвое больше байтов там, где
    плитка всё равно квадратная.

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
        response = await client.get(_thumb_url(file, width), headers=headers)
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


def _thumb_url(file: dict | str, width: int) -> str:
    """Адрес превью нужного размера — из записи Диска либо из одного номера.

    Запись несёт `thumbnailLink` с собственным суффиксом (`=s220`), и просить
    другой размер означает суффикс **заменить**, а не дописать второй:
    `=s220=s400` Диск не понимает и отвечает прежней картинкой.

    Номера довольно и без записи — см. `GOOGLE_DRIVE_THUMB_BY_ID`. Этой дорогой
    и ходят показывающие: у них на руках свой индекс, а не свежий ответ Диска.
    """
    if isinstance(file, dict) and file.get('thumbnailLink'):
        return file['thumbnailLink'].split('=s')[0] + f'=s{width}'

    file_id = file['id'] if isinstance(file, dict) else str(file)
    return GOOGLE_DRIVE_THUMB_BY_ID.format(file_id=file_id, size=width)


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

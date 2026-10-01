"""Чтение Google Диска сервисным аккаунтом: токен, запрос, список файлов.

Дверь наружу к Drive API v3 и только к нему. Здесь нет знания о том, что за
файлы читаются и зачем, — есть «спросить Диск и принести ответ».

**Почему сервисный аккаунт, а не личный.** Личный требует согласия человека в
браузере, и это не формальность, а три отдельные платы, измеренные живьём:

- `redirect_uri` надо зарегистрировать заранее и совпасть строкой — иначе
  `Error 400: redirect_uri_mismatch` ещё до окна входа;
- refresh-токен в проекте со статусом *Testing* живёт **семь дней** независимо
  от использования, дальше `invalid_grant` и новое согласие руками;
- выпуск в продакшен со scope `drive.readonly` — это верификация приложения и
  ежегодная оценка безопасности (CASA), потому что читаются чужие файлы.

Сервисный аккаунт снимает все три: он объект проекта, а не человек, токен берёт
подписью ключа, и видит **только то, что ему расшарили**, — а не весь Диск
владельца. Цена ровно одна: папку расшаривают на его адрес руками, один раз.

⚠⚠ **«Нам ничего не расшарили» выглядит как «папка пуста».** Аккаунту видно
только расшаренное, поэтому забытое «Поделиться» отвечает не отказом, а пустым
списком. Отличить можно: `google_drive_list('trashed = false')` без прочих
условий у настроенного доступа **не бывает** пустым. Тому, кто показывает это
человеку, стоит сказать адрес аккаунта и слово «Поделиться», а не «папка не
найдена»: иначе он пойдёт переименовывать папки, которых не видит.

⚠ **Ключ — путём, а не значением.** Приватный ключ в переменной окружения читает
всякий, кто видит окружение процесса; в репозитории ему нельзя тем более.
`GOOGLE_DRIVE_KEY` указывает на файл, выданный Google Cloud Console.

Обход страниц, рекурсивный обход дерева и байты — в соседних модулях
(`google_drive_walk.py`, `google_drive_bytes.py`). Тема целиком —
`docs/google_drive.md`.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx

from config.config import config_get
from http_.http_pool import http_pool_transport

GOOGLE_DRIVE_API = 'https://www.googleapis.com/drive/v3'
GOOGLE_DRIVE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'

# Поля ответа `files.list`. Просить «всё» (`files(*)`) дороже и шумнее, а этот
# набор отвечает на вопросы, ради которых к Диску и ходят: что это, какого размера,
# менялось ли (`md5Checksum`), есть ли готовое превью.
GOOGLE_DRIVE_FIELDS = (
    'nextPageToken,files(id,name,mimeType,size,md5Checksum,createdTime,'
    'modifiedTime,thumbnailLink,imageMediaMetadata,videoMediaMetadata)'
)

# Папка — такой же файл, отличается типом. Константа, потому что строку эту
# пишут в каждом втором запросе и опечатка в ней выглядит как «папок нет».
GOOGLE_DRIVE_FOLDER_MIME = 'application/vnd.google-apps.folder'

# Потолок страниц обхода: 1000 записей на страницу, то есть 50 000 файлов. Это
# не ожидаемый объём, а предохранитель от бесконечного `pageToken` при ошибке
# на стороне Диска — лучше отдать неполный список, чем ходить вечно.
GOOGLE_DRIVE_PAGES_MAX = 50

# Запас на обрыв: токен, которому осталось жить меньше двух минут, уже не берём —
# иначе длинный обход уходит в `401` на середине.
GOOGLE_DRIVE_TOKEN_SPARE = 120

_tokens: dict[str, tuple[str, float]] = {}


async def google_drive_token(key_path: str = '', scope: str = '') -> str:
    """Токен доступа к Диску по ключу сервисного аккаунта — без браузера и согласия.

    Подписывает JWT приватным ключом из JSON-файла Google Cloud Console и меняет
    его на access-токен. Токен кэшируется в памяти процесса до истечения срока,
    поэтому звать функцию перед каждым запросом дёшево и правильно.

    Args:
        key_path: путь к JSON-ключу. Пусто — берётся `GOOGLE_DRIVE_KEY` из
            настроек проекта.
        scope: область доступа. Пусто — чтение всего, что расшарено
            (`drive.readonly`).

    Returns:
        Строка токена, годная для заголовка `Authorization: Bearer`.

    Raises:
        RuntimeError: ключа нет, он не читается, или Google отказал.

    ⚠ **Токен наружу не отдают.** Он годится полчаса и открывает всё, что видит
    аккаунт: ни в URL картинки, ни в ответ ручки, ни в журнал он попадать не
    должен — только в заголовок запроса.
    """
    path = _key_path(key_path)

    cached, until = _tokens.get(path, ('', 0.0))
    if cached and until > time.time():
        return cached

    token, expires = await _token_fetch(path, scope or GOOGLE_DRIVE_SCOPE)
    _tokens[path] = (token, time.time() + max(expires - GOOGLE_DRIVE_TOKEN_SPARE, 30))
    return token


async def google_drive_get(path: str, params: dict | None = None, key_path: str = '') -> dict:
    """Запрос к Drive API v3 с токеном сервисного аккаунта; ответ — разобранный JSON.

    Args:
        path: путь внутри API без ведущего слэша — например `files`.
        params: параметры строки запроса.
        key_path: путь к ключу; пусто — из настроек.

    Raises:
        RuntimeError: Диск ответил не `200`. Текст отказа Google приводится как
            есть, но отдельно распознаётся выключенный в проекте Drive API —
            по нему иначе гадают, глядя на `403`.
    """
    token = await google_drive_token(key_path)
    client = httpx.AsyncClient(timeout=60, transport=http_pool_transport())
    response = await client.get(f'{GOOGLE_DRIVE_API}/{path.lstrip("/")}',
                                params=params or {},
                                headers={'Authorization': f'Bearer {token}'})
    if response.status_code != 200:
        raise RuntimeError(_error_words(response.status_code, response.text))
    return response.json()


async def google_drive_list(query: str, fields: str = '', key_path: str = '',
                            order_by: str = 'name_natural',
                            pages_max: int = GOOGLE_DRIVE_PAGES_MAX) -> list[dict]:
    """Файлы, подходящие под запрос Диска, — все страницы одним списком.

    Args:
        query: условие Диска (`q`), например `'<id>' in parents and trashed = false`.
        fields: поля ответа; пусто — `GOOGLE_DRIVE_FIELDS`.
        key_path: путь к ключу; пусто — из настроек.
        order_by: порядок; по умолчанию «как человек читает имена» — чтобы
            список не прыгал между обновлениями.
        pages_max: потолок страниц, предохранитель от бесконечного `pageToken`.

    Returns:
        Список записей Диска. **Пустой список — законный ответ**, но см.
        предупреждение в шапке модуля: у сервисного аккаунта он же означает
        «ничего не расшарили».

    ⚠ **Кавычки в `query` не экранируются.** Подставлять туда имя, пришедшее от
    человека, нельзя: одна кавычка ломает условие. Идентификаторы Диска
    безопасны (их выдал сам Диск), имена — ищите обходом
    (`google_drive_walk`) и сравнивайте в Python.
    """
    out: list[dict] = []
    page = ''
    for _ in range(max(pages_max, 1)):
        params = {
            'q': query,
            'fields': fields or GOOGLE_DRIVE_FIELDS,
            'pageSize': 1000,
            'orderBy': order_by,
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        }
        if page:
            params['pageToken'] = page
        answer = await google_drive_get('files', params, key_path)
        out += answer.get('files', [])
        page = answer.get('nextPageToken', '')
        if not page:
            break
    return out


def google_drive_list_wait(query: str, fields: str = '', key_path: str = '') -> list[dict]:
    """Синхронный вход в `google_drive_list` — для скриптов, CLI и тестов."""
    return asyncio.run(google_drive_list(query, fields, key_path))


async def _token_fetch(key_path: str, scope: str) -> tuple[str, int]:
    """Подписать JWT ключом аккаунта и обменять его на access-токен.

    Тяжёлый криптостек подключается здесь, а не на уровне модуля: список файлов
    читают чаще, чем обновляют токен, и тянуть `cryptography` ради импорта
    модуля незачем.
    """
    import base64

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        key = json.loads(Path(key_path).read_text(encoding='utf-8'))
    except OSError as err:
        raise RuntimeError(f'ключ сервисного аккаунта не читается: {err}') from err
    if key.get('type') != 'service_account':
        raise RuntimeError(f'это не ключ сервисного аккаунта: type={key.get("type")!r}')

    now = int(time.time())
    chunk = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=')
    head = chunk(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    body = chunk(json.dumps({'iss': key['client_email'], 'scope': scope,
                             'aud': GOOGLE_DRIVE_TOKEN_URL,
                             'iat': now, 'exp': now + 3600}).encode())
    private = serialization.load_pem_private_key(key['private_key'].encode(), password=None)
    signature = private.sign(head + b'.' + body, padding.PKCS1v15(), hashes.SHA256())

    client = httpx.AsyncClient(timeout=30, transport=http_pool_transport())
    response = await client.post(GOOGLE_DRIVE_TOKEN_URL, data={
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': (head + b'.' + body + b'.' + chunk(signature)).decode(),
    })
    if response.status_code != 200:
        raise RuntimeError(f'Google не выдал токен ({response.status_code}): {response.text[:300]}')
    answer = response.json()
    return answer['access_token'], int(answer.get('expires_in', 3600))


def _key_path(key_path: str = '') -> str:
    """Путь к ключу: заданный вызовом либо `GOOGLE_DRIVE_KEY` из настроек.

    ⚠⚠ **Относительный путь считается от корня проекта, а не от рабочего
    каталога.** В настройках его естественно писать коротко
    (`google_drive/ключ.json`), и из запущенной панели такой путь работает —
    она стартует из корня. А вот задание планировщика, сюита или разовый
    скрипт запускаются откуда угодно, и тот же путь у них не открывается:
    отказ выглядит как «ключа нет», хотя ключ на месте.

    Проверено делом: задание галереи из рабочего каталога ветки получило
    «No such file or directory» на все пятьдесят три анкеты.
    """
    path = str(key_path or config_get('GOOGLE_DRIVE_KEY', ''))
    if not path:
        raise RuntimeError('ключ сервисного аккаунта не задан: GOOGLE_DRIVE_KEY')

    if Path(path).is_absolute():
        return path

    from project_.project_ import project_root

    return str(project_root() / path)


def _error_words(status: int, text: str) -> str:
    """Отказ Диска словами: выключенный API опознаётся отдельно от прочих `403`."""
    if 'has not been used in project' in text or 'accessNotConfigured' in text:
        return ('Google Drive API в проекте не включён — '
                'APIs & Services → Enable → Google Drive API')
    return f'Диск отказал ({status}): {text[:300]}'


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Чтение Google Диска сервисным аккаунтом.')
    parser.add_argument('command', choices=['token', 'list', 'shared'],
                        help='token — проверить доступ; list — файлы по условию `q`; '
                             'shared — всё, что видно аккаунту (пусто = не расшарили)')
    parser.add_argument('argument', nargs='?', default='', help='условие `q` для list')
    parser.add_argument('--key', default='', help='путь к JSON-ключу; пусто — GOOGLE_DRIVE_KEY')
    args = parser.parse_args()

    try:
        if args.command == 'token':
            got = asyncio.run(google_drive_token(args.key))
            print(f'токен получен, длина {len(got)}')
        else:
            where = args.argument if args.command == 'list' and args.argument else 'trashed = false'
            rows = asyncio.run(google_drive_list(where, key_path=args.key))
            if not rows:
                print('видно ноль файлов — вероятно, папку не расшарили на адрес аккаунта')
            for row in rows:
                mark = '📁' if row['mimeType'] == GOOGLE_DRIVE_FOLDER_MIME else '📄'
                print(f'{mark} {row["id"]}  {row["name"]}  {row["mimeType"]}')
    except RuntimeError as err:
        raise SystemExit(f'ошибка: {err}')

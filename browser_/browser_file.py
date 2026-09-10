"""Файлы: подложить свой в поле формы и забрать скачанный.

Без этого сценарий упирается в стену на самом обычном месте: загрузить фотографию в
профиль, приложить документ, скачать выгрузку. Кликнуть по `<input type="file">`
нельзя — браузер откроет системное окно выбора файла, которого в контейнере нет и
которым никто не управляет.

Обе стороны работают с **файловой системой того контейнера, где живёт браузер**, и
это главное, что нужно про них знать. Путь `/app/data/photo.jpg` в шаге сценария
— это путь внутри браузерного контейнера, а не на машине человека и не на том
сервере, откуда пришёл запрос. Файл туда попадает общим томом или тем же
скачиванием.

⚠ **Имя скачанного файла приходит с сайта, то есть от чужого.** Оно проходит через
`_safe_name`: каталоги отбрасываются целиком, из остатка выживают только буквы,
цифры и три знака. Иначе `../../app/main.py` в заголовке `Content-Disposition`
записался бы поверх нашего кода — на одной строке кода разницы, на всю систему
последствий.
"""

import os
import re
import time

from config.config import config_get
from logging_.logging_ import logger_info

# Куда падает скачанное. Своя папка, а не системная `/tmp`: файлы живут между
# шагами сценария, и уборщик системного каталога забрал бы их из-под ног.
BROWSER_FILE_DIR = config_get('BROWSER_DOWNLOAD_DIR', '/tmp/browser_download')

# Сколько файлов принимаем за одну загрузку. Поле с `multiple` бывает, но десятками
# в него не грузят, а список путей приезжает строкой через запятую — и опечатка в
# ней не должна превращаться в сотню обращений к диску.
BROWSER_FILE_UPLOAD_LIMIT = 10

# Сколько ждём начала скачивания. Кнопка «Выгрузить» на сервере считает отчёт, и
# полминуты здесь — не запас, а обычное время такого ответа.
BROWSER_FILE_DOWNLOAD_TIMEOUT_MS = 60000

# Чем заменяем всё, чего в имени файла быть не должно. `\w` с флагом Unicode:
# сайты отдают файлы с именами на своём языке («отчёт.pdf»), и приводить их к
# `_______.pdf` значило бы терять единственное, чем эти файлы различают.
BROWSER_FILE_NAME_RE = re.compile(r'[^\w.-]+', re.UNICODE)

BROWSER_FILE_NAME_LIMIT = 100


async def browser_file_upload(page, selector: str, paths: str,
                              timeout_ms: int = 10000) -> dict:
    """
    Подкладывает файл (или несколько) в поле выбора файла.

    Args:
        page: страница Playwright.
        selector: `input[type=file]` — либо то, что открывает окно выбора (кнопка,
            подпись, картинка). Разбираться, что именно, здесь не нужно: см. ниже.
        paths: путь внутри контейнера браузера; несколько — через запятую.
        timeout_ms: сколько ждать сам элемент.

    Returns:
        dict: `files` — имена подложенных, `count`, `chooser` — пришлось ли ловить
        окно выбора.

    Raises:
        FileNotFoundError: файла нет на диске контейнера. Проверяем **до** обращения
            к странице: Playwright на несуществующий путь отвечает многострочной
            ошибкой про сам вызов, из которой не следует ничего.

    ⚠ **Два разных пути к одной цели, и выбирает страница, а не мы.** У аккуратной
    формы есть настоящий `<input type="file">`, и ему файлы просто присваивают —
    без клика, без окна, надёжно. У формы с картинкой вместо кнопки настоящий
    `input` спрятан или создаётся кликом, и присвоить некому: тогда остаётся нажать
    и поймать окно выбора (`expect_file_chooser`). Сначала пробуем первый способ —
    он не зависит от вёрстки, — и только если элемент оказался не полем, идём вторым.
    """
    files = _paths_check(paths)

    locator = page.locator(selector)
    kind = await _element_kind(locator, timeout_ms)

    if kind == 'file-input':
        await locator.set_input_files(files, timeout=timeout_ms)
        logger_info(f'[browser] загружено файлов: {len(files)} в {selector[:60]}')
        return {'files': [os.path.basename(path) for path in files],
                'count': len(files), 'chooser': False}

    # Окно выбора файла ловится **до** клика, а не после: страница успевает открыть
    # и закрыть его быстрее, чем сюда вернётся управление, и подписка задним числом
    # не увидит ничего.
    async with page.expect_file_chooser(timeout=timeout_ms) as info:
        await locator.click(timeout=timeout_ms)
    chooser = await info.value
    await chooser.set_files(files)

    logger_info(f'[browser] загружено файлов через окно выбора: {len(files)}')

    return {'files': [os.path.basename(path) for path in files],
            'count': len(files), 'chooser': True}


async def browser_file_download(page, selector: str = '',
                                timeout_ms: int = BROWSER_FILE_DOWNLOAD_TIMEOUT_MS,
                                directory: str = '') -> dict:
    """
    Нажимает на то, что скачивает файл, и забирает файл себе.

    Args:
        page: страница Playwright.
        selector: по чему нажать. Пусто — ничего не нажимать, а просто ждать: бывает,
            что скачивание запускает предыдущий шаг или сама страница по таймеру.
        timeout_ms: сколько ждать начала скачивания.
        directory: куда положить; пусто — `BROWSER_FILE_DIR`.

    Returns:
        dict: `name` — имя файла, `path` — где он лёг, `size` — байты, `url` —
        откуда пришёл, `suggested` — как его назвал сайт (до приведения имени).

    Raises:
        TimeoutError: скачивание не началось. Именно так и выглядит сломанный
            сценарий: кнопка нажалась, а файла нет — сайт ответил страницей с
            ошибкой вместо файла.

    ⚠ **Ждать надо начав ждать, а не нажав.** `expect_download` ставится вокруг
    клика, потому что маленький файл успевает скачаться до возврата из `click()`, и
    подписка после клика упустила бы событие целиком.

    ⚠ **Файл существует, пока жива страница.** Playwright держит скачанное во
    временном каталоге и выбрасывает вместе с контекстом; `save_as` переносит его в
    нашу папку, и только после этого путь можно кому-то отдавать.
    """
    directory = directory or BROWSER_FILE_DIR
    os.makedirs(directory, exist_ok=True)

    try:
        async with page.expect_download(timeout=timeout_ms) as info:
            if selector:
                await page.locator(selector).click(timeout=timeout_ms)
        download = await info.value
    except Exception as e:
        raise TimeoutError(f'скачивание не началось: {str(e).splitlines()[0]}')

    suggested = str(download.suggested_filename or 'download')
    name = _safe_name(suggested)
    path = os.path.join(directory, f'{int(time.time())}_{name}')

    await download.save_as(path)
    size = os.path.getsize(path) if os.path.exists(path) else 0

    logger_info(f'[browser] скачан файл {name}: {size} байт')

    return {'name': name, 'path': path, 'size': size,
            'url': str(download.url or ''), 'suggested': suggested[:BROWSER_FILE_NAME_LIMIT]}


def _paths_check(paths: str) -> list[str]:
    """Строка путей → список существующих файлов. Пустое и лишнее отсекается здесь."""
    files = [item.strip() for item in str(paths or '').split(',') if item.strip()]
    if not files:
        raise ValueError('upload: не указан ни один файл')
    if len(files) > BROWSER_FILE_UPLOAD_LIMIT:
        raise ValueError(f'upload: больше {BROWSER_FILE_UPLOAD_LIMIT} файлов за раз не берём')

    missing = [path for path in files if not os.path.isfile(path)]
    if missing:
        raise FileNotFoundError(
            f'нет файла в контейнере браузера: {", ".join(missing[:3])}')

    return files


async def _element_kind(locator, timeout_ms: int) -> str:
    """`file-input` — настоящее поле выбора файла, `other` — всё прочее.

    Ждём элемент здесь же: дальше оба пути начинаются с обращения к нему, и
    отдельное ожидание в каждом задваивало бы таймаут шага.
    """
    await locator.first.wait_for(state='attached', timeout=timeout_ms)

    try:
        return await locator.first.evaluate(
            '(el) => (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "file")'
            '        ? "file-input" : "other"')
    except Exception:
        return 'other'


def _safe_name(name: str) -> str:
    """
    Имя файла, пришедшее с сайта, — в безопасное имя.

    Каталоги отбрасываются целиком (`basename`), затем остаётся только то, из чего
    имя файла и состоит. Пустое после чистки заменяется на `download`: файл без
    имени положить некуда, а падать здесь — терять уже скачанное.
    """
    name = os.path.basename(str(name or '').strip())
    name = BROWSER_FILE_NAME_RE.sub('_', name).strip('._')

    return (name or 'download')[:BROWSER_FILE_NAME_LIMIT]

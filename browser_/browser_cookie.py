"""Вход сессии: сохранить, вернуть, пересказать обычному HTTP-клиенту.

`storage_state` — это cookie и `localStorage` контекста, то есть **сам факт того,
что мы вошли**. Пул умеет отдать его и принять обратно (`browser_pool_storage_state`,
`browser_pool_session_open`), и на этом круг замыкается: следующая сессия
открывается уже авторизованной, без прохода по форме входа. Здесь — три вещи,
которых в этом круге не хватало.

**Пережить рестарт.** Реестр сессий живёт в памяти, и вход умирает вместе с
контейнером. Проход по форме входа стоит десятков секунд и иногда кода из письма —
платить этим за каждый перезапуск не нужно, и файл со состоянием решает вопрос.

**Уйти из браузера.** Перехват сети (`net_watch`) отвечает на вопрос, каким
запросом страница делает своё дело; ответ бесполезен, пока нечем повторить этот
запрос от имени той же сессии. `browser_cookie_header` даёт ровно это: строку
`Cookie:` для обычного HTTP-клиента. Дальше вкладка на 143 МБ заменяется запросом
на десятки килобайт — та же экономия, ради которой заведён `browser_ws`.

**Знать, когда вход умрёт.** У cookie есть срок, и «сценарий вдруг перестал
работать» почти всегда означает «вход истёк вчера». `browser_cookie_expiry`
отвечает на это до того, как сломается сценарий.

⚠ **Файл со входом равен паролю.** По нему входят без пароля и без второго
фактора, поэтому каталог держат рядом с секретами, а не в общем томе, и наружу
такой файл не отдают никогда — ни ответом API, ни в журнал.
"""

import json
import os
import re
import time
from urllib.parse import urlsplit

from config.config import config_get
from logging_.logging_ import logger_info

# Куда складываем состояния входа. Отдельный каталог, а не общий с загрузками:
# у файлов разная цена (см. предупреждение выше), и путать их не следует.
BROWSER_COOKIE_DIR = config_get('BROWSER_STATE_DIR', '/tmp/browser_state')

# Имя файла складывается из имени состояния; всё, чего в имени быть не должно,
# заменяется. Имя приходит запросом, и `../` в нём означало бы запись куда угодно.
#
# `\w` с флагом Unicode, а не латиница: состояния называет человек, и «проба»
# превращалась в `_` целиком — файл получал пустое имя и запись отвергалась. Букв
# алфавита бояться нечего, опасны разделители пути, и их здесь не остаётся.
BROWSER_COOKIE_NAME_RE = re.compile(r'[^\w.-]+', re.UNICODE)

BROWSER_COOKIE_NAME_LIMIT = 60

# Срок, ближе которого вход считается «скоро умрёт». Сутки: этого хватает, чтобы
# заметить и обновить вход до того, как по нему упадёт ночной сценарий.
BROWSER_COOKIE_SOON_SEC = 24 * 3600


def browser_cookie_header(storage_state: dict, url: str) -> str:
    """
    Строка `Cookie:` для обычного HTTP-клиента — те cookie, что браузер послал бы на `url`.

    Отбор такой же, как у самого браузера, и каждое условие здесь не для порядка:
    домен (иначе уедет чужая cookie, а вместе с ней чужая сессия), путь (у сайта
    бывает своя cookie на `/admin`), признак `secure` (по http такую не отдают), и
    срок — истёкшую сервер не примет, но обязательно ответит так, будто мы не
    вошли, и разбираться придётся с симптомом.

    Returns:
        `name=value; name2=value2`. Пусто — на этот адрес отдавать нечего.
    """
    parsed = urlsplit(str(url or ''))
    host = (parsed.hostname or '').lower()
    path = parsed.path or '/'
    secure = parsed.scheme == 'https'
    now = time.time()

    pairs = []
    for cookie in _cookies(storage_state):
        if not _domain_match(host, str(cookie.get('domain') or '')):
            continue
        if not path.startswith(str(cookie.get('path') or '/')):
            continue
        if cookie.get('secure') and not secure:
            continue
        expires = float(cookie.get('expires') or -1)
        if 0 < expires < now:
            continue
        name = str(cookie.get('name') or '')
        if name:
            pairs.append(f'{name}={cookie.get("value", "")}')

    return '; '.join(pairs)


def browser_cookie_netscape(storage_state: dict) -> str:
    """
    Все cookie в формате `cookies.txt` — том, который понимают `curl` и его родня.

    Нужен там, где запрос делает не наш код: выкачать файл, который отдаётся только
    вошедшему, проверить ответ сервера руками из консоли. Формат старый, но он
    единственный общий у всех этих инструментов.

    Первая строка — обязательная метка формата: без неё `curl` файл не читает.
    """
    lines = ['# Netscape HTTP Cookie File', '# сохранено browser_cookie']

    for cookie in _cookies(storage_state):
        domain = str(cookie.get('domain') or '')
        if not domain:
            continue
        # Второй столбец — «распространяется ли на поддомены». В формате это ровно
        # то же, что точка в начале домена, но записано отдельным словом.
        subdomains = 'TRUE' if domain.startswith('.') else 'FALSE'
        secure = 'TRUE' if cookie.get('secure') else 'FALSE'
        expires = int(float(cookie.get('expires') or 0))
        # Сессионная cookie (срока нет) в этом формате пишется нулём.
        expires = expires if expires > 0 else 0
        lines.append('\t'.join([domain, subdomains, str(cookie.get('path') or '/'),
                                secure, str(expires), str(cookie.get('name') or ''),
                                str(cookie.get('value') or '')]))

    return '\n'.join(lines) + '\n'


def browser_cookie_expiry(storage_state: dict) -> dict:
    """
    Когда умрёт вход: по самой ранней cookie со сроком.

    Считаем по **самой ранней**, а не по последней: сессию держит обычно одна
    cookie, и её смерть означает выход, сколько бы ни осталось жить остальным.

    Returns:
        dict: `expires_at` (строкой, пусто — все сессионные), `in_sec`, `in_days`,
        `soon` — умрёт в пределах суток, `session_only` — сколько cookie без срока
        (они умирают вместе с закрытием контекста, и файл их не спасёт),
        `total`, `expired` — сколько уже истекло.
    """
    now = time.time()
    soonest = 0.0
    session_only = 0
    expired = 0
    total = 0

    for cookie in _cookies(storage_state):
        total += 1
        expires = float(cookie.get('expires') or -1)
        if expires <= 0:
            session_only += 1
            continue
        if expires < now:
            expired += 1
            continue
        if soonest == 0.0 or expires < soonest:
            soonest = expires

    left = round(soonest - now) if soonest else 0

    return {
        'expires_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(soonest)) if soonest else '',
        'in_sec': left,
        'in_days': round(left / 86400, 1) if left else 0,
        'soon': bool(soonest) and left <= BROWSER_COOKIE_SOON_SEC,
        'session_only': session_only,
        'expired': expired,
        'total': total,
    }


def browser_cookie_state_save(storage_state: dict, name: str) -> str:
    """
    Кладёт состояние входа в файл. Возвращает путь.

    Запись идёт через временный файл с переименованием: перезапись на месте
    оставляет полуфайл, если процесс погасили посреди неё, — а полуфайл со входом
    неотличим от испорченного, и обнаружится это на следующем запуске, когда войти
    уже нужно.
    """
    path = _path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    temporary = f'{path}.tmp'
    with open(temporary, 'w', encoding='utf-8') as f:
        json.dump(storage_state or {}, f, ensure_ascii=False)
    os.replace(temporary, path)

    # Права 600: файл равен паролю, и общий том его не должен показывать соседям.
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger_info(f'[browser] права на файл входа не выставились: {e}')

    logger_info(f'[browser] состояние входа сохранено: {os.path.basename(path)}, '
                f'cookie {len(_cookies(storage_state))}')

    return path


def browser_cookie_state_load(name: str) -> dict:
    """
    Читает состояние входа из файла. Нет файла — пустой словарь, а не исключение.

    Пустой словарь потому, что «входа нет» — это законный ответ, с которого
    начинается любая первая сессия: вызывающий пройдёт по форме входа и сохранит
    состояние сам. Исключение здесь заставляло бы каждого оборачивать вызов в
    `try`, чтобы получить ровно этот же пустой словарь.
    """
    path = _path(name)
    if not os.path.isfile(path):
        return {}

    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
    except Exception as e:
        # Испорченный файл — это не «входа нет», и молчать здесь нельзя: сценарий
        # пойдёт по форме входа и будет прав, но причину надо видеть в журнале.
        logger_info(f'[browser] файл входа не прочитался: {e}')
        return {}

    return state if isinstance(state, dict) else {}


def browser_cookie_state_list() -> list[dict]:
    """Какие состояния входа лежат на диске: имя, размер, когда сохранено."""
    directory = BROWSER_COOKIE_DIR
    if not os.path.isdir(directory):
        return []

    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.json'):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        out.append({'name': name[:-len('.json')], 'bytes': stat.st_size,
                    'saved_at': time.strftime('%Y-%m-%d %H:%M:%S',
                                              time.localtime(stat.st_mtime))})

    return out


def _cookies(storage_state: dict) -> list[dict]:
    """Список cookie из состояния. Чужая форма — пустой список, а не падение:
    состояние приходит и телом запроса, и из файла, и из чужого браузера."""
    cookies = (storage_state or {}).get('cookies')

    return [item for item in cookies if isinstance(item, dict)] if isinstance(cookies, list) else []


def _domain_match(host: str, domain: str) -> bool:
    """Подходит ли cookie этому хосту.

    Точка в начале домена значит «и поддомены тоже»: `.example.com` уезжает и на
    `api.example.com`. Без точки cookie принадлежит ровно одному хосту — так её и
    сохранил браузер, и расширять это правило самим значило бы отдавать cookie
    туда, куда её не отдал бы он.
    """
    domain = domain.lower().strip()
    if not domain or not host:
        return False

    if domain.startswith('.'):
        bare = domain[1:]
        return host == bare or host.endswith(f'.{bare}')

    return host == domain


def _path(name: str) -> str:
    """Путь к файлу состояния по его имени. Имя чистится: оно приходит запросом."""
    clean = BROWSER_COOKIE_NAME_RE.sub('_', os.path.basename(str(name or '').strip()))
    clean = clean.strip('._')[:BROWSER_COOKIE_NAME_LIMIT]
    if not clean:
        raise ValueError('имя состояния входа пусто')

    return os.path.join(BROWSER_COOKIE_DIR, f'{clean}.json')

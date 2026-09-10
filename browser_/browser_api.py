"""HTTP-API контейнера-браузера: завести сессию, увести её на URL, погасить.

Команды приходят по HTTP, а не через очередь: у них есть ответ, которого ждёт
вызывающий («сессия открыта, вот её id»). События в обратную сторону — изменения
DOM (`browser_watch`), журнал консоли — копятся в сессии, и их забирают опросом:
ждать их некому, а сокет ради этого держать дороже, чем спросить.

Прав здесь нет в том смысле, в каком они есть у веб-приложения: сервис не для
человека, он говорит только с бэкендом по внутренней сети. Наружу порт публикуется
на петлевой адрес и только для отладки. `BROWSER_API_TOKEN` включает проверку
заголовка, если сервис однажды окажется доступен шире.

Ручки поверх открытой страницы — текст, снимок, наблюдатель, консоль, вход —
живут в `browser_api_page`: этот файл про сами сессии.
"""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from config.config import config_get

from browser_.browser_memory import browser_memory_stat
from browser_.browser_pool import (
    browser_pool_session_close,
    browser_pool_session_goto,
    browser_pool_session_list,
    browser_pool_session_open,
    browser_pool_stat,
    browser_pool_storage_state,
)
from browser_.browser_thumb import browser_thumb_forget, browser_thumb_get

router = APIRouter()

BROWSER_API_TOKEN = config_get('BROWSER_API_TOKEN', '')
BROWSER_API_WAIT_UNTIL = ('commit', 'domcontentloaded', 'load', 'networkidle')


class BrowserSessionCreate(BaseModel):
    name: str = Field('', max_length=100, description='Человеческое имя сессии для списка')
    site_id: int = Field(0, ge=0, description='Метка вызывающего: чья это сессия')
    url: str = Field('', description='Куда сразу перейти; пусто — остаться на about:blank')
    storage_state: dict | None = Field(None, description='Cookie и localStorage прошлой сессии')
    state_name: str = Field('', max_length=60,
                            description='Взять вход из сохранённого файла (browser_cookie)')
    viewport: dict | None = Field(None, description='{"width": …, "height": …}')
    locale: str = Field('', max_length=20, description='Язык интерфейса сайта, напр. ru-RU')
    timezone_id: str = Field('', max_length=60, description='Пояс страницы, напр. Europe/Kyiv')

    @field_validator('url')
    @classmethod
    def _url_check(cls, value: str) -> str:
        return _url_normalize(value, required=False)


class BrowserSessionGoto(BaseModel):
    url: str
    wait_until: str = 'domcontentloaded'
    timeout_ms: int = Field(30000, ge=1000, le=180000)

    @field_validator('url')
    @classmethod
    def _url_check(cls, value: str) -> str:
        return _url_normalize(value, required=True)

    @field_validator('wait_until')
    @classmethod
    def _wait_until_check(cls, value: str) -> str:
        if value not in BROWSER_API_WAIT_UNTIL:
            raise ValueError(f'wait_until: ожидается одно из {", ".join(BROWSER_API_WAIT_UNTIL)}')
        return value


async def browser_api_token_check(x_browser_token: str = Header('')):
    """Проверка заголовка, если токен задан в окружении. Не задан — проверки нет."""
    if BROWSER_API_TOKEN and x_browser_token != BROWSER_API_TOKEN:
        raise HTTPException(status_code=401, detail='неверный токен')


# Токен проверяется на всём, кроме `/browser/health`: на health смотрит
# healthcheck контейнера, и заголовки ему подсунуть некуда.
browser_api_auth = [Depends(browser_api_token_check)]


@router.get('/browser/health')
async def browser_api_health():
    """Живость сервиса. Без токена — на этот роут смотрит healthcheck контейнера."""
    return {'status': 'ok', 'pool': browser_pool_stat()}


@router.get('/browser/session', dependencies=browser_api_auth)
async def browser_api_session_list():
    """Список сессий вместе с памятью — тем, ради чего список и смотрят."""
    sessions = browser_pool_session_list()
    return {
        'sessions': sessions,
        'total': len(sessions),
        'pool': browser_pool_stat(),
        'memory': browser_memory_stat(),
    }


@router.post('/browser/session', status_code=201, dependencies=browser_api_auth)
async def browser_api_session_create(data: BrowserSessionCreate):
    """Заводит сессию: изолированный контекст и одна страница в нём.

    Вход можно передать телом (`storage_state`) либо назвать по имени
    (`state_name`) — тогда он читается из файла, сохранённого раньше. Второе
    существует ради того, чтобы вызывающему не приходилось таскать через себя
    чужие cookie: он знает имя, а сам вход не покидает контейнера.
    """
    storage_state = data.storage_state
    if storage_state is None and data.state_name:
        # Читаем лениво: файлов может не быть вовсе, а модуль работы с ними
        # незачем тянуть в каждый запрос за списком сессий.
        from browser_.browser_cookie import browser_cookie_state_load

        try:
            storage_state = browser_cookie_state_load(data.state_name) or None
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if storage_state is None:
            raise HTTPException(status_code=404,
                                detail=f'сохранённого входа «{data.state_name}» нет')

    try:
        session = await browser_pool_session_open(
            name=data.name,
            url=data.url,
            storage_state=storage_state,
            viewport=data.viewport,
            site_id=data.site_id,
            locale=data.locale,
            timezone_id=data.timezone_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ConnectionError as e:
        # Сессия не открылась на переходе — пул её уже погасил, а вызывающему
        # нужна причина: 502, как и на отдельном `/goto`. До этого здесь был 500
        # и незакрытая вкладка.
        raise HTTPException(status_code=502, detail=str(e))

    return {'session': session}


@router.delete('/browser/session/{session_id}', dependencies=browser_api_auth)
async def browser_api_session_delete(session_id: str):
    """Гасит сессию. Повторный вызов не ошибка: результат тот же — сессии нет."""
    closed = await browser_pool_session_close(session_id)
    browser_thumb_forget(session_id)
    return {'session_id': session_id, 'closed': closed}


@router.get('/browser/session/{session_id}/thumb', dependencies=browser_api_auth)
async def browser_api_session_thumb(session_id: str, fresh: bool = False):
    """Миниатюра страницы сессии — узнать вкладку в лицо.

    Кадр снимается по запросу и кэшируется: он стоит полного рендера страницы, а
    карточек в колонке до двух с половиной десятков.

    Неудачный снимок приходит с кодом 200 и текстом в `error`: страница могла
    зависнуть на своём JavaScript — ровно такие в списке и интересны, — и ронять
    из-за неё весь список нечем.
    """
    try:
        return await browser_thumb_get(session_id, fresh=fresh)
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')


@router.post('/browser/session/{session_id}/goto', dependencies=browser_api_auth)
async def browser_api_session_goto(session_id: str, data: BrowserSessionGoto):
    """Переводит страницу сессии на URL."""
    try:
        session = await browser_pool_session_goto(
            session_id, data.url, wait_until=data.wait_until, timeout_ms=data.timeout_ms)
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        # Сюда попадают сбои самой страницы: таймаут, недоступный хост, обрыв.
        # Это не наша ошибка и не 500 — вызывающий должен увидеть причину.
        raise HTTPException(status_code=502, detail=f'страница не открылась: {e}')

    return {'session': session}


@router.get('/browser/session/{session_id}/storage_state', dependencies=browser_api_auth)
async def browser_api_session_storage_state(session_id: str):
    """Cookie и localStorage сессии — чтобы следующая открылась уже авторизованной."""
    try:
        state = await browser_pool_storage_state(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')

    return {'storage_state': state}


def _url_normalize(value: str, required: bool) -> str:
    value = (value or '').strip()
    if not value:
        if required:
            raise ValueError('url обязателен')
        return ''
    if not value.startswith(('http://', 'https://')):
        raise ValueError('url должен начинаться с http:// или https://')
    return value

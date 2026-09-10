"""HTTP-API поверх открытой страницы: текст, снимок, наблюдатель, консоль, вход.

Всё, что можно спросить у **уже живущей** сессии, не трогая ни её адрес, ни её
состояние. Отдельно от `browser_api`, потому что вопрос там другой: тот файл про
сессии как таковые (завести, увести, погасить), этот — про содержимое страницы.

Ручки собраны в одном месте, а не разложены по своим модулям, намеренно: сами
модули (`browser_read`, `browser_capture`, `browser_watch`, `browser_console`,
`browser_cookie`) обходятся без FastAPI, и их можно звать из скрипта, из
сценария, из чужого процесса — там, где никакого HTTP нет вовсе.

Коды ответов — про запрос, а не про содержимое: нет сессии — 404, страница
закрыта — 409. Неудача чтения или снимка приходит с кодом **200** и текстом в
`error`: спрашивают такое обычно как раз тогда, когда со страницей что-то не так,
и 500 отобрал бы единственный способ на неё посмотреть.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from browser_.browser_api import browser_api_auth
from browser_.browser_capture import (BROWSER_CAPTURE_QUALITY, browser_capture_pdf,
                                      browser_capture_shot)
from browser_.browser_console import (browser_console_info, browser_console_rows,
                                      browser_console_session_start,
                                      browser_console_session_stop)
from browser_.browser_cookie import (browser_cookie_expiry, browser_cookie_header,
                                     browser_cookie_netscape, browser_cookie_state_list,
                                     browser_cookie_state_save)
from browser_.browser_pool import (browser_pool_session_get, browser_pool_storage_state)
from browser_.browser_read import BROWSER_READ_TEXT_LIMIT, browser_read_page
from browser_.browser_watch import (browser_watch_info, browser_watch_start,
                                    browser_watch_stop, browser_watch_take)

router = APIRouter()


class BrowserPageRead(BaseModel):
    selector: str = Field('body', max_length=500, description='Корень чтения — сузить дешевле, чем обрезать')
    links: bool = Field(False, description='Собрать заодно ссылки страницы')
    limit: int = Field(BROWSER_READ_TEXT_LIMIT, ge=100, le=200000)


class BrowserPageCapture(BaseModel):
    selector: str = Field('', max_length=500, description='Вырезка по элементу; пусто — окно')
    full_page: bool = Field(False, description='Страница целиком, с прокруткой')
    quality: int = Field(BROWSER_CAPTURE_QUALITY, ge=10, le=100)


class BrowserPageWatch(BaseModel):
    selector: str = Field('body', max_length=500, description='За каким поддеревом смотреть')
    attributes: bool = Field(False, description='Считать и правку атрибутов — обычно это шум')
    limit: int = Field(0, ge=0, le=5000, description='0 — предел по умолчанию')


class BrowserPageConsole(BaseModel):
    filter: str = Field('', max_length=200, description='Писать только сообщения с этим текстом')
    limit: int = Field(0, ge=0, le=5000, description='0 — предел по умолчанию')


class BrowserPageStateSave(BaseModel):
    name: str = Field(..., min_length=1, max_length=60, description='Под каким именем сохранить вход')


@router.post('/browser/session/{session_id}/read', dependencies=browser_api_auth)
async def browser_api_page_read(session_id: str, data: BrowserPageRead):
    """Читаемый текст страницы — то, что видит человек, без разметки."""
    entry = _entry(session_id)

    return await browser_read_page(entry['page'], selector=data.selector,
                                   links=data.links, limit=data.limit)


@router.post('/browser/session/{session_id}/capture', dependencies=browser_api_auth)
async def browser_api_page_capture(session_id: str, data: BrowserPageCapture):
    """Кадр страницы или элемента строкой `data:`."""
    entry = _entry(session_id)

    return await browser_capture_shot(entry['page'], selector=data.selector,
                                      full_page=data.full_page, quality=data.quality)


@router.post('/browser/session/{session_id}/pdf', dependencies=browser_api_auth)
async def browser_api_page_pdf(session_id: str, landscape: bool = False):
    """Страница как PDF: текст остаётся текстом, разбиение на страницы — за браузером."""
    entry = _entry(session_id)

    return await browser_capture_pdf(entry['page'], landscape=landscape)


@router.post('/browser/session/{session_id}/watch/start', dependencies=browser_api_auth)
async def browser_api_page_watch_start(session_id: str, data: BrowserPageWatch):
    """Включает наблюдение за DOM. Повторный вызов начинает наблюдение заново."""
    entry = _entry(session_id)

    options = {'selector': data.selector, 'attributes': data.attributes}
    if data.limit:
        options['limit'] = data.limit

    return {'watch': await browser_watch_start(entry, **options)}


@router.post('/browser/session/{session_id}/watch/stop', dependencies=browser_api_auth)
async def browser_api_page_watch_stop(session_id: str):
    """Гасит наблюдение и отдаёт последнюю пачку — ту, ради которой обычно и гасят."""
    entry = _entry(session_id)
    rows = await browser_watch_stop(entry)

    return {'rows': rows, 'total': len(rows)}


@router.get('/browser/session/{session_id}/watch', dependencies=browser_api_auth)
async def browser_api_page_watch_get(session_id: str, clear: bool = True):
    """Что изменилось с прошлого опроса. `clear=false` — не забирать, только посмотреть."""
    entry = _entry(session_id)
    rows = browser_watch_take(entry, clear=clear)

    return {'rows': rows, 'total': len(rows), 'watch': browser_watch_info(entry.get('watch'))}


@router.post('/browser/session/{session_id}/console/start', dependencies=browser_api_auth)
async def browser_api_page_console_start(session_id: str, data: BrowserPageConsole):
    """Включает журнал консоли на всю сессию — со всеми её окнами."""
    entry = _entry(session_id)

    options = {'text_filter': data.filter}
    if data.limit:
        options['limit'] = data.limit
    state = browser_console_session_start(entry, **options)

    return {'console': browser_console_info(state)}


@router.post('/browser/session/{session_id}/console/stop', dependencies=browser_api_auth)
async def browser_api_page_console_stop(session_id: str):
    """Гасит журнал и отдаёт накопленное."""
    entry = _entry(session_id)
    rows = browser_console_session_stop(entry)

    return {'rows': rows, 'total': len(rows)}


@router.get('/browser/session/{session_id}/console', dependencies=browser_api_auth)
async def browser_api_page_console_get(session_id: str, filter: str = '', kinds: str = ''):
    """Журнал страницы: `kinds=error,pageerror` — только то, из-за чего падают шаги."""
    entry = _entry(session_id)
    state = entry.get('console')
    rows = browser_console_rows(state, text_filter=filter, kinds=kinds)

    return {'rows': rows, 'total': len(rows), 'console': browser_console_info(state)}


@router.get('/browser/session/{session_id}/cookie', dependencies=browser_api_auth)
async def browser_api_page_cookie(session_id: str, url: str = ''):
    """
    Вход сессии в виде, пригодном для обычного HTTP-клиента.

    ⚠ **Здесь отдаётся сама сессия, а не сведения о ней.** Заголовок `Cookie` равен
    паролю: по нему входят без пароля и без второго фактора. Ручка живёт за токеном
    и предназначена своему же коду — тому, который заменяет дорогой прогон
    сценария одним запросом (`net_watch` показывает, каким именно).
    """
    _entry(session_id)

    try:
        state = await browser_pool_storage_state(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')

    return {
        'header': browser_cookie_header(state, url) if url else '',
        'netscape': browser_cookie_netscape(state),
        'expiry': browser_cookie_expiry(state),
    }


@router.post('/browser/session/{session_id}/cookie/save', dependencies=browser_api_auth)
async def browser_api_page_cookie_save(session_id: str, data: BrowserPageStateSave):
    """
    Складывает вход сессии в файл — чтобы он пережил перезапуск контейнера.

    Обратный ход — `state_name` при заведении сессии (`POST /browser/session`):
    она откроется уже вошедшей, без прохода по форме.
    """
    _entry(session_id)

    try:
        state = await browser_pool_storage_state(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')

    try:
        path = browser_cookie_state_save(state, data.name)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {'name': data.name, 'path': path, 'expiry': browser_cookie_expiry(state)}


@router.get('/browser/cookie', dependencies=browser_api_auth)
async def browser_api_page_cookie_list():
    """Какие входы лежат на диске: имя, размер, когда сохранён."""
    states = browser_cookie_state_list()

    return {'states': states, 'total': len(states)}


def _entry(session_id: str) -> dict:
    """Запись сессии или отказ. Закрытая страница — 409: сессия есть, но работать
    с ней нечем, и 404 сказал бы неправду."""
    entry = browser_pool_session_get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail='сессия не найдена')
    if entry['page'].is_closed():
        raise HTTPException(status_code=409, detail='страница сессии закрыта')

    return entry

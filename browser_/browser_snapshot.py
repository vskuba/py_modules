"""Снимок страницы для LLM: дерево доступности плюс устойчивые селекторы.

Скриншот модели не годится — по картинке нельзя написать шаг сценария, там нет
ни ролей, ни имён, ни селекторов. HTML не годится тем более: страница админки это
сотни килобайт разметки, из которых значимы полтора десятка строк. Playwright
умеет отдавать дерево доступности в режиме `ai` — то же, что видит скринридер:
роли, подписи, состояния, по строке на элемент.

К каждому элементу дерева Playwright приписывает ссылку `[ref=e12]`. Ссылка живёт
до следующей навигации и годится, чтобы **осмотреться и потыкать** здесь и сейчас;
в сохранённый сценарий её класть нельзя — завтра тот же элемент будет `e17`.
Поэтому снимок дополняется вторым полем: `sel="#password"` — селектор, вычисленный
на самой странице и проверенный на единственность (`browser_selector`). Его и
записывает автор сценария.

Снимок отвечает на вопрос «за что здесь можно нажать». На вопрос «что здесь
написано» отвечает `browser_read`: текст письма или объявления из дерева
доступности не собрать, там подписи, а не содержание.
"""

import asyncio
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from logging_.logging_ import logger_info

from browser_.browser_api import browser_api_auth
from browser_.browser_pool import (
    browser_pool_session_close,
    browser_pool_session_get,
    browser_pool_session_open,
)
from browser_.browser_selector import BROWSER_SELECTOR_ALL_JS

router = APIRouter()

# Роли, с которыми вообще можно что-то сделать. Селектор считается только для них:
# на странице списка задач полсотни интерактивных элементов и тысячи `generic`,
# и вычислять селектор для каждого пустого `div` — это секунды на снимок впустую.
BROWSER_SNAPSHOT_ROLES_INTERACTIVE = {
    'button', 'link', 'textbox', 'searchbox', 'combobox', 'listbox', 'option',
    'checkbox', 'radio', 'switch', 'slider', 'spinbutton', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'tab', 'treeitem',
}

# Предел на число элементов с селектором: каждый — это обращение к странице за
# дескриптором. Полсотни хватает любой форме, а на списке из тысячи строк снимок
# останется быстрым — модели всё равно нужны первые, а не тысячная.
BROWSER_SNAPSHOT_ELEMENT_LIMIT = 60

# Предел на текст снимка. Дерево большой страницы не влезает в разумный запрос к
# модели, а обрезанное сверху вниз — влезает и остаётся осмысленным: разметка идёт
# в порядке документа, и начало страницы почти всегда важнее её подвала.
BROWSER_SNAPSHOT_TEXT_LIMIT = 20000

# Дескриптор берётся с коротким ожиданием: ссылка либо разрешается сразу, либо она
# устарела (страница ушла), и ждать её десять секунд бессмысленно.
BROWSER_SNAPSHOT_HANDLE_TIMEOUT_MS = 1000

# `- textbox "Имя пользователя" [ref=e9]:` — роль, подпись и ссылка.
BROWSER_SNAPSHOT_LINE_RE = re.compile(r'^\s*-\s+(?P<role>[a-zA-Z]+)(?:\s+"(?P<name>.*?)")?\s+\[ref=(?P<ref>e\d+)\]')


class BrowserSnapshotRequest(BaseModel):
    """Что снимать и где."""

    session_id: str = Field('', description='Пусто — завести временную сессию под снимок')
    url: str = Field('', max_length=1000, description='Перейти перед снимком; для временной сессии обязателен')
    selector: str = Field('body', max_length=500, description='Корень поддерева — снять только форму, а не всю страницу')
    interactive_only: bool = Field(True, description='Селекторы только для кликабельного; выключать редко зачем')
    depth: int = Field(0, ge=0, le=20, description='0 — всё дерево целиком')
    keep_session: bool = Field(False, description='Не гасить временную сессию — посмотреть её в живом окне')

    @field_validator('url')
    @classmethod
    def _url_check(cls, value: str) -> str:
        value = (value or '').strip()
        if value and not value.startswith(('http://', 'https://')):
            raise ValueError('url должен начинаться с http:// или https://')
        return value


@router.post('/browser/snapshot', dependencies=browser_api_auth)
async def browser_snapshot_post(data: BrowserSnapshotRequest):
    """Снимает дерево страницы. Коды — про запрос: 404 сессия, 422 нет url, 502 страница."""
    try:
        return await browser_snapshot_take(
            session_id=data.session_id,
            url=data.url,
            selector=data.selector,
            interactive_only=data.interactive_only,
            depth=data.depth,
            keep_session=data.keep_session,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail='сессия не найдена')
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ConnectionError as e:
        # Сбой самой страницы — недоступный хост, таймаут, обрыв. Не наша ошибка
        # и не 500: вызывающий должен увидеть причину и решить, повторять ли.
        raise HTTPException(status_code=502, detail=str(e))


async def browser_snapshot_take(session_id: str = '', url: str = '', selector: str = 'body',
                                interactive_only: bool = True, depth: int = 0,
                                keep_session: bool = False) -> dict:
    """Дерево доступности с ссылками `ref` и вычисленными селекторами.

    Временная сессия гасится после снимка, чужая — никогда: снимок часто делают со
    страницы, которую в этот момент смотрит человек в живом окне.
    """
    if not session_id and not url:
        raise ValueError('нужен session_id или url')

    entry, own_session = await _session_take(session_id, url)
    page = entry['page']

    try:
        if url and not own_session:
            try:
                await page.goto(url, wait_until='domcontentloaded')
            except Exception as e:
                raise ConnectionError(f'страница не открылась: {e}')

        try:
            text = await page.locator(selector or 'body').aria_snapshot(mode='ai', depth=depth or None)
        except Exception as e:
            # Чаще всего это строгий режим: селектор корня совпал с несколькими
            # элементами. Это ошибка запроса, а не сбой — 422, а не 500.
            raise ValueError(f'селектор «{selector}» не подошёл: {str(e).splitlines()[0]}')

        elements = _lines_parse(text, interactive_only)
        selectors = await _selectors_compute(page, [item['ref'] for item in elements])

        for item in elements:
            item['selector'] = selectors.get(item['ref']) or _role_selector(item['role'], item['name'])
        await _role_selectors_verify(page, elements)

        result = {
            'session_id': entry['session_id'],
            'session_kept': not own_session or keep_session,
            'url': page.url,
            'title': await page.title(),
            'snapshot': _annotate(text, {item['ref']: item['selector'] for item in elements}),
            'elements': elements,
        }
    finally:
        if own_session and not keep_session:
            await browser_pool_session_close(entry['session_id'])

    result['truncated'] = len(result['snapshot']) > BROWSER_SNAPSHOT_TEXT_LIMIT
    result['snapshot'] = result['snapshot'][:BROWSER_SNAPSHOT_TEXT_LIMIT]

    logger_info(f'[browser] снимок {result["url"]}: элементов {len(elements)}, '
                f'{len(result["snapshot"])} символов')

    return result


async def _session_take(session_id: str, url: str) -> tuple[dict, bool]:
    """Сессия снимка и признак «завели её мы»."""
    if session_id:
        entry = browser_pool_session_get(session_id)
        if entry is None:
            raise KeyError(session_id)
        if entry['page'].is_closed():
            raise RuntimeError('страница сессии закрыта')
        return entry, False

    session = await browser_pool_session_open(name='снимок')
    entry = browser_pool_session_get(session['session_id'])

    if url:
        try:
            await entry['page'].goto(url, wait_until='domcontentloaded')
        except Exception as e:
            # Сессию за собой убираем сами: наверху её ещё нет, и `finally` до неё
            # не доберётся — открытая страница осталась бы висеть до перезапуска.
            await browser_pool_session_close(session['session_id'])
            raise ConnectionError(f'страница не открылась: {e}')

    return entry, True


def _lines_parse(text: str, interactive_only: bool) -> list[dict]:
    """Строки снимка → элементы. Порядок документа сохраняется — он и есть порядок чтения."""
    elements: list[dict] = []

    for line in text.splitlines():
        match = BROWSER_SNAPSHOT_LINE_RE.match(line)
        if not match:
            continue

        role = match.group('role')
        if interactive_only and role not in BROWSER_SNAPSHOT_ROLES_INTERACTIVE:
            continue

        elements.append({'ref': match.group('ref'), 'role': role,
                         'name': match.group('name') or '', 'selector': ''})
        if len(elements) >= BROWSER_SNAPSHOT_ELEMENT_LIMIT:
            break

    return elements


async def _selectors_compute(page, refs: list[str]) -> dict[str, str]:
    """Селекторы для ссылок одним заходом в страницу.

    Дескрипторы собираются параллельно, а считаются все разом: сотня отдельных
    `evaluate` на элемент стоила бы сотни обращений к браузеру вместо одного.
    """
    if not refs:
        return {}

    handles = await asyncio.gather(*[_handle_get(page, ref) for ref in refs])
    pairs = [(ref, handle) for ref, handle in zip(refs, handles) if handle is not None]
    if not pairs:
        return {}

    try:
        values = await page.evaluate(BROWSER_SELECTOR_ALL_JS, [handle for _, handle in pairs])
    except Exception as e:
        # Снимок без селекторов всё ещё полезен: роли и подписи на месте, а
        # селектор подставится от роли. Ронять из-за этого весь ответ незачем.
        logger_info(f'[browser] селекторы не вычислились: {e}')
        values = []
    finally:
        await asyncio.gather(*[handle.dispose() for _, handle in pairs], return_exceptions=True)

    return {ref: value for (ref, _), value in zip(pairs, values) if value}


async def _handle_get(page, ref: str):
    """Дескриптор по ссылке снимка или None, если ссылка устарела."""
    try:
        return await page.locator(f'aria-ref={ref}').element_handle(
            timeout=BROWSER_SNAPSHOT_HANDLE_TIMEOUT_MS)
    except Exception:
        return None


def _role_selector(role: str, name: str) -> str:
    """Запасной селектор по роли и подписи — когда на элементе нет ни одного атрибута.

    Он читаемый и переживает смену вёрстки, но ломается от перевода интерфейса,
    поэтому и стоит последним. Без подписи не выдаётся вовсе: `role=generic`
    совпадёт с половиной страницы, и польза от такой подсказки отрицательная —
    автор сценария примет её за селектор.
    """
    return f'role={role}[name="{name.replace(chr(34), chr(92) + chr(34))}"]' if name else ''


async def _role_selectors_verify(page, elements: list[dict]):
    """Проверяет запасные селекторы на единственность, неоднозначные — стирает.

    Кандидаты в CSS проверены на самой странице, а роль с подписью — нет: одинаковых
    «Сохранить» на странице бывает три. Пустой селектор честнее: автор сценария
    увидит, что элемент не адресуется, и возьмёт родителя.
    """
    checked = [item for item in elements if item['selector'].startswith('role=')]
    if not checked:
        return

    counts = await asyncio.gather(*[_selector_count(page, item['selector']) for item in checked])

    for item, count in zip(checked, counts):
        if count != 1:
            item['selector'] = ''


async def _selector_count(page, selector: str) -> int:
    """Сколько элементов совпало. Ошибка селектора — это ноль, а не исключение."""
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


def _annotate(text: str, selectors: dict[str, str]) -> str:
    """Дописывает селектор рядом со ссылкой: `[ref=e9]` → `[ref=e9 sel="#username"]`.

    Оба поля в одной строке, потому что читать модели придётся именно её: отдельный
    список «ссылка → селектор» она сопоставляет с деревом с ошибками.
    """
    if not selectors:
        return text

    def replace(match):
        ref = match.group(1)
        selector = selectors.get(ref)

        return f'[ref={ref} sel="{selector}"]' if selector else match.group(0)

    return re.sub(r'\[ref=(e\d+)\]', replace, text)

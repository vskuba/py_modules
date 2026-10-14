"""Разведка страницы под сценарий: какие на ней поля, кнопки и чем их звать.

Сценарий пишут по живой странице, а не по догадке из её HTML. Разметка
интернет-магазинов наполовину собирается скриптом: форма быстрого заказа лежит
в модалке, которой в исходном коде нет вовсе, а имена классов у темы свои.
Открыть, нажать, посмотреть — единственный надёжный путь, и этот модуль его
сокращает до одного вызова.

## ⚠⚠ Отвечает СЕЛЕКТОРАМИ, а не текстом

Текст страницы (`read`) отвечает на вопрос «что человек видит», а сценарию
нужно «чем это звать». Снимок доступности даёт второе: у каждого элемента есть
роль (`textbox`, `button`), имя (подпись или placeholder) и готовый селектор.
Из этого сценарий собирается почти механически.

## ⚠ Подготовительные шаги — часть вопроса

Половина форм не существует до нажатия: «Швидке замовлення» открывает модалку,
корзина — боковую панель. Поэтому `steps` выполняются **до** снимка, в той же
сессии, и это обычный список шагов сценария (`browser_scenario.py`).

## ⚠ Роли, а не теги

`<div contenteditable>` — тоже поле ввода, а `<a>` с обработчиком — тоже
кнопка. Снимок доступности видит их такими, какими их видит человек и
скринридер; разбор по именам тегов на этом ошибается регулярно.
"""

from browser_.browser_session import browser_session_close
from browser_.browser_upstream import browser_upstream_request

# Что интересно тому, кто пишет сценарий. ⚠ Список открытый: лишняя роль здесь
# дешевле недостающей — отобрать из показанного человек может сам, а чего не
# показали, того он не узнает.
BROWSER_PROBE_ROLES = (
    'textbox', 'searchbox', 'button', 'link', 'combobox', 'checkbox',
    'radio', 'listbox', 'option', 'switch', 'spinbutton',
)

# Сколько ждём ответа. Разведка идёт по живому сайту с его скриптами и
# модалками — она медленнее обычного запроса и это нормально.
BROWSER_PROBE_TIMEOUT = 120.0


async def browser_probe_page(url: str, steps: list | None = None,
                             roles: tuple = BROWSER_PROBE_ROLES,
                             proxy: str = '', selector: str = '') -> dict:
    """Что на странице: поля, кнопки и чем их звать в сценарии.

    Args:
        url: адрес страницы.
        steps: шаги, которые надо выполнить до снимка, — тем же видом, что и в
            сценарии (`[{'action': 'click', 'selector': '.btn-fastorder'}]`).
            Пусто — снимок страницы как она открылась.
        roles: какие роли показывать; пусто — все, какие есть.
        selector: корень поддерева — снять только его. ⚠ Не удобство: снимок
            страницы магазина **обрезается** контейнером на шестом десятке
            элементов, и форма в модалке в него просто не попадает. Поймано на
            первом же разборе: шаги отработали, модалка открылась, а в ответе
            было меню и подвал.
        proxy: адрес выхода строкой `ip:port`. ⚠ Разведка идёт туда же, куда
            потом пойдёт сценарий: сайт может отдавать разную разметку разным
            странам, и писать сценарий по чужой — значит писать не тот.

    Returns:
        `{'url', 'title', 'elements': [{'role', 'name', 'selector'}, …],
          'error'}`. Отказ приходит полем, а не исключением: разведку зовут из
        страницы панели, и чужой сайт не обязан отвечать.

    ⚠ Элементы фильтруются по ролям, но **не** сортируются: порядок снимка —
    порядок на странице, и по нему видно, что за чем идёт.
    """
    body = {'url': str(url or ''), 'keep_session': bool(steps)}
    if proxy:
        body['proxy'] = proxy

    try:
        # Шаги и снимок идут одной сессией: модалка, открытая шагом, к моменту
        # снимка должна быть ещё на экране.
        session_id = ''
        if steps:
            # ⚠ Переход добавляется сюда, а не ждётся от зовущего: функция
            # получила адрес — значит она и открывает. Первый прогон без этого
            # падал «Locator.click: Timeout» на пустой вкладке, и выглядело это
            # как неверный селектор, а не как незагруженная страница.
            walk = [{'action': 'goto', 'url': str(url or '')}] + list(steps)
            run = await browser_upstream_request(
                'POST', '/browser/scenario/run',
                {'scenario': {'name': 'разведка', 'goal': '', 'steps': walk},
                 'variables': {}, 'secrets': {}, 'session_id': '',
                 'keep_session': True, 'proxy': proxy},
                timeout=BROWSER_PROBE_TIMEOUT)
            if run.get('status') != 'ok':
                return {'url': url, 'title': '', 'elements': [],
                        'error': str(run.get('error') or 'шаги не прошли')}
            session_id = str(run.get('session_id') or '')

        shot = await browser_upstream_request(
            'POST', '/browser/snapshot',
            {'session_id': session_id, 'url': '' if session_id else url,
             'selector': selector},
            timeout=BROWSER_PROBE_TIMEOUT)
    except Exception as fault:
        return {'url': url, 'title': '', 'elements': [], 'error': str(fault)[:200]}
    finally:
        if steps and (session_id := locals().get('session_id', '')):
            # ⚠ Сессию закрываем всегда: брошенная держит вкладку Chromium до
            # перезапуска контейнера, и на двадцатой разведке он кончается.
            await browser_session_close(session_id)

    return {'url': shot.get('url') or url, 'title': shot.get('title') or '',
            'elements': _probe_fit(shot.get('elements') or [], roles),
            'error': ''}


# ── детали реализации ──────────────────────────────────────────────────────

def _probe_fit(elements: list, roles: tuple) -> list:
    """Оставить то, из чего пишут сценарий, и только нужные поля записи."""
    return [{'role': row.get('role', ''), 'name': row.get('name', ''),
             'selector': row.get('selector', '')}
            for row in elements
            if (not roles or row.get('role') in roles) and row.get('selector')]


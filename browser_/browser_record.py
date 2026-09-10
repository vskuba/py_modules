"""Запись сценария за человеком: он работает в живом окне, мы собираем шаги.

Автор-агент пишет сценарий по цели, а здесь обратный ход: человек показывает
руками, что нужно сделать, и получает готовый черновик. Так дешевле всего
описать проход, который трудно объяснить словами, — незаметная кнопка, порядок
полей, промежуточная страница.

Слушатели живут на самой странице (`add_init_script` + `expose_binding`): только
там видно, на какой элемент пришёлся клик и что оказалось в поле. Ни то, ни
другое из CDP-события мыши не вывести — там одни координаты.

Ни привязку, ни init-скрипт Playwright снять не умеет. Поэтому ставятся они один
раз на сессию, а «идёт запись или нет» решает флаг на стороне Python: страница
шлёт события всегда, и лишние мы просто выбрасываем.

Селектор считается тем же кодом, что и у снимка (`browser_selector`): шаг,
записанный за человеком, и шаг, написанный агентом по снимку, обязаны адресовать
элемент одинаково — иначе один сценарий чинился бы двумя способами.
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from logging_.logging_ import logger_info

from browser_.browser_api import browser_api_auth
from browser_.browser_pool import browser_pool_pages, browser_pool_session_get
from browser_.browser_scenario import BROWSER_SCENARIO_STEP_LIMIT
from browser_.browser_selector import BROWSER_SELECTOR_ONE_JS

router = APIRouter()

BROWSER_RECORD_BINDING = '__browserRecordEmit'
BROWSER_RECORD_SELECTOR_LIMIT = 500
BROWSER_RECORD_VALUE_LIMIT = 2000
BROWSER_RECORD_KEY_LIMIT = 50

# Пароль в сценарий не попадает: сценарии лежат в базе и видны в админке. На его
# место встаёт подстановка — значение подставят на прогоне из списка ключей.
BROWSER_RECORD_SECRET_VALUE = '{{password}}'

BROWSER_RECORD_ACTIONS = ('click', 'fill', 'select', 'press', 'goto')

# Ставится на каждый документ сессии — включая переходы и дочерние окна.
BROWSER_RECORD_JS = """
(() => {
  // Флаг на документе, а не на окне: у всплывшего окна `window` переживает
  // навигацию, а `document` — нет. С флагом на `window` скрипт считал окно
  // вооружённым и выходил, тогда как слушатели остались на предыдущем документе
  // (обычно на `about:blank`, с которого popup начинается) — и клики в дочерней
  // вкладке не записывались вовсе.
  if (document.__browserRecordOn) return;
  document.__browserRecordOn = true;

  const selectorOf = __SELECTOR_ONE__;

  // Клик почти всегда приходит на внутренность кнопки (span, svg, текст), а
  // сценарию нужен сам орган управления — от него селектор и переживёт правку вёрстки.
  const INTERACTIVE = 'a,button,input,select,textarea,label,[role="button"],[role="link"],'
    + '[role="tab"],[role="menuitem"],[role="checkbox"],[role="radio"],[onclick]';

  // Флажки и кнопки в этот список не входят: у них меняется не значение, а
  // состояние, и повторяет его именно клик, а не подстановка значения.
  const SKIP_TYPES = ['hidden', 'file', 'checkbox', 'radio', 'button', 'submit', 'reset', 'image'];
  const KEYS = ['Enter', 'Escape'];

  const last = new Map();
  const send = (step) => { try { window.__browserRecordEmit(step); } catch (e) {} };

  const fieldOf = (el) => {
    if (!el || !el.tagName) return null;
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return el;
    if (tag === 'input' && !SKIP_TYPES.includes((el.type || '').toLowerCase())) return el;
    return null;
  };

  const fillSend = (el) => {
    const sel = selectorOf(el);
    if (!sel) return;
    const value = el.value == null ? '' : String(el.value);
    if (last.get(sel) === value) return;
    last.set(sel, value);
    const secret = (el.type || '').toLowerCase() === 'password';
    send({action: 'fill', selector: sel, value: secret ? '__SECRET__' : value});
  };

  // Перехват на погружении: страница может остановить всплытие своим обработчиком,
  // и тогда до нас событие не дошло бы вовсе.
  document.addEventListener('click', (event) => {
    // `isTrusted` отсекает клики, которые страница сделала сама: клик по метке
    // подставляет второй клик по полю, и в сценарий попал бы лишний шаг.
    if (!event.isTrusted) return;
    const target = event.target;
    if (!target || !target.closest) return;
    const sel = selectorOf(target.closest(INTERACTIVE) || target);
    // `detail` — счётчик кликов мыши; у клика, который браузер сделал сам в ответ
    // на клавишу, он равен нулю. По нему Python и отличит отправку формы с Enter.
    if (sel) send({action: 'click', selector: sel, detail: event.detail});
  }, true);

  document.addEventListener('change', (event) => {
    const el = event.target;
    if (!el || !el.tagName) return;
    if (el.tagName.toLowerCase() === 'select') {
      const sel = selectorOf(el);
      if (sel) send({action: 'select', selector: sel, value: el.value == null ? '' : String(el.value)});
      return;
    }
    const field = fieldOf(el);
    if (field) fillSend(field);
  }, true);

  document.addEventListener('keydown', (event) => {
    if (!event.isTrusted || !KEYS.includes(event.key)) return;
    // Enter отправляет форму, и `change` набранного поля придёт уже после
    // нажатия — а то и не придёт вовсе, если страница успеет уйти. Значит,
    // поле надо записать прямо сейчас, до шага с клавишей.
    const field = fieldOf(document.activeElement);
    if (field) fillSend(field);
    send({action: 'press', key: event.key});
  }, true);
})();
""".replace('__SELECTOR_ONE__', BROWSER_SELECTOR_ONE_JS).replace(
    '__SECRET__', BROWSER_RECORD_SECRET_VALUE)


class BrowserRecordStart(BaseModel):
    name: str = Field('', max_length=100, description='Имя будущего сценария — для журнала записи')


@router.post('/browser/session/{session_id}/record/start', dependencies=browser_api_auth)
async def browser_record_start_post(session_id: str, data: BrowserRecordStart):
    """Включает запись действий в сессии."""
    entry = _entry_get(session_id)

    try:
        await _listeners_install(entry)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'не удалось включить запись: {e}')

    entry['record'] = {
        'name': data.name,
        'started_at': time.time(),
        'start_url': entry['page'].url,
        'steps': [],
        'limit_hit': False,
    }
    logger_info(f'[browser] запись начата: {session_id} ({data.name or "без имени"})')

    return {'record': _record_info(entry['record'])}


@router.post('/browser/session/{session_id}/record/stop', dependencies=browser_api_auth)
async def browser_record_stop_post(session_id: str):
    """Останавливает запись и отдаёт собранные шаги."""
    entry = _entry_get(session_id)

    record = entry.pop('record', None)
    if record is None:
        raise HTTPException(status_code=409, detail='запись не велась')

    logger_info(f'[browser] запись остановлена: {session_id}, шагов {len(record["steps"])}')
    return {'record': _record_info(record, steps=True)}


@router.get('/browser/session/{session_id}/record', dependencies=browser_api_auth)
async def browser_record_get(session_id: str):
    """Состояние записи: идёт ли и сколько шагов набралось."""
    entry = _entry_get(session_id)
    record = entry.get('record')

    if record is None:
        return {'record': None}
    return {'record': _record_info(record, steps=True)}


def browser_record_goto(session_id: str, url: str):
    """Отмечает переход, набранный в адресной строке живого окна.

    Переходы по ссылкам сюда не идут: их уже записал клик, и `goto` рядом с ним
    только сломал бы сценарий — он увёл бы страницу до того, как клик сработал.
    """
    entry = browser_pool_session_get(session_id)
    if entry is None:
        return

    record = entry.get('record')
    if record is not None:
        _step_add(record, {'action': 'goto', 'url': url})


def _entry_get(session_id: str) -> dict:
    entry = browser_pool_session_get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail='сессия не найдена')
    return entry


async def _listeners_install(entry: dict):
    """Ставит привязку и init-скрипт — один раз на сессию.

    Init-скрипт достаётся только новым документам, поэтому уже открытые страницы
    вооружаются отдельным `evaluate`: запись должна начаться там, где человек
    сейчас стоит, а не со следующего перехода. Страниц может быть несколько —
    сайт открывает дочерние окна, и показывают проход часто именно в них.
    """
    context = entry['context']
    session_id = entry['session_id']

    if not entry.get('record_installed'):
        def emit(source, step):
            _emit(session_id, step)

        await context.expose_binding(BROWSER_RECORD_BINDING, emit)
        await context.add_init_script(BROWSER_RECORD_JS)

        # Одного init-скрипта всплывшему окну не хватает: до того, как он там
        # отработает, страница уже успевает принять клики, и первые действия
        # человека в новом окне теряются. Подписка догоняет окно и вооружает его
        # явно — тем же приёмом, что и инспектор. Снять её нечем, поэтому ставится
        # один раз на сессию.
        def on_page(page):
            asyncio.create_task(_page_arm(page))

        context.on('page', on_page)
        entry['record_installed'] = True

    # Вооружаем все открытые страницы, а не только первую: к началу записи у сессии
    # уже может быть всплывшее дочернее окно, и человек показывает проход именно в
    # нём. Новые окна init-скрипт получат сами.
    for page in browser_pool_pages(entry):
        try:
            await page.evaluate(BROWSER_RECORD_JS)
        except Exception as e:
            logger_info(f'[browser] запись: скрипт не встал на страницу: {e}')


async def _page_arm(page) -> None:
    """Вооружает всплывшее окно: ждёт документ и ставит слушатели.

    Скрипт защищён от повторного запуска (`document.__browserRecordOn`), так что
    столкнуться с init-скриптом здесь не страшно — второй раз он просто выйдет.
    """
    try:
        await page.wait_for_load_state('domcontentloaded')
        if not page.is_closed():
            await page.evaluate(BROWSER_RECORD_JS)
    except Exception as e:
        logger_info(f'[browser] запись: всплывшее окно не вооружилось: {e}')


def _emit(session_id: str, step):
    """Событие со страницы. Сессии может уже не быть — страница живёт своей жизнью."""
    entry = browser_pool_session_get(session_id)
    if entry is None:
        return

    record = entry.get('record')
    if record is not None and isinstance(step, dict):
        _step_add(record, step)


def _step_add(record: dict, step: dict):
    """Кладёт шаг в запись, приводя его к схеме сценария."""
    keyboard_click = step.get('action') == 'click' and not step.get('detail')

    step = _step_clean(step)
    if step is None:
        return

    steps = record['steps']

    # Enter в форме браузер превращает в нажатие кнопки отправки — приходит и
    # клавиша, и клик по кнопке. На прогоне это уже два действия: Enter уводит
    # страницу, и кнопку следом искать негде. Оставляем клик — он говорит, по
    # чему именно нажали, а Enter об этом умалчивает.
    if keyboard_click and steps and steps[-1].get('action') == 'press' \
            and steps[-1].get('key') == 'Enter':
        steps[-1] = step
        return

    # Человек правит поле по буквам, а иногда и возвращается к нему: в сценарии
    # должно остаться итоговое значение, а не история набора.
    if step['action'] == 'fill' and steps and steps[-1].get('action') == 'fill' \
            and steps[-1].get('selector') == step['selector']:
        steps[-1] = step
        return

    if len(steps) >= BROWSER_SCENARIO_STEP_LIMIT:
        record['limit_hit'] = True
        return

    steps.append(step)


def _step_clean(step: dict) -> dict | None:
    """Отсекает мусор со страницы: чужие поля, пустые цели, слишком длинные значения."""
    action = str(step.get('action') or '')
    if action not in BROWSER_RECORD_ACTIONS:
        return None

    clean = {'action': action}

    if action == 'goto':
        url = str(step.get('url') or '').strip()[:1000]
        if not url:
            return None
        clean['url'] = url
        return clean

    if action == 'press':
        key = str(step.get('key') or '').strip()[:BROWSER_RECORD_KEY_LIMIT]
        if not key:
            return None
        clean['key'] = key
        return clean

    selector = str(step.get('selector') or '').strip()[:BROWSER_RECORD_SELECTOR_LIMIT]
    if not selector:
        return None
    clean['selector'] = selector

    if action in ('fill', 'select'):
        # Схема сценария требует непустое значение, а очистка поля приходит сюда
        # пустой строкой. Такой шаг выбрасывается целиком: иначе одно случайно
        # стёртое поле обрушило бы проверку и вместе с ней всю запись.
        value = str(step.get('value') or '')[:BROWSER_RECORD_VALUE_LIMIT]
        if not value.strip():
            return None
        clean['value'] = value

    return clean


def _record_info(record: dict, steps: bool = False) -> dict:
    """Запись в виде, пригодном для JSON.

    Первым шагом дописывается переход на стартовый адрес: сценарий прогоняют на
    чистой сессии, и без него он начался бы с about:blank.
    """
    info = {
        'name': record['name'],
        'start_url': record['start_url'],
        'step_count': len(record['steps']),
        'limit_hit': record['limit_hit'],
        'duration_sec': round(time.time() - record['started_at']),
    }

    if steps:
        out = list(record['steps'])
        start_url = record['start_url']
        if start_url.startswith(('http://', 'https://')) and (not out or out[0].get('action') != 'goto'):
            out.insert(0, {'action': 'goto', 'url': start_url})
        info['steps'] = out

    return info

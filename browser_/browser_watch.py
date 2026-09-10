"""Наблюдатель DOM: страница сама говорит, что на ней появилось.

⚠ **Это то, ради чего браузер вынесен в отдельный процесс и живёт неделями.**
Всё остальное — снимок, сценарий, живое окно — работает по запросу: мы спросили,
страница ответила. Наблюдатель работает наоборот: он молчит часами, а потом
сообщает, что в чате пришло сообщение, в списке появилась новая строка, а на
кнопке сменился текст. Опрашивать то же самое сценарием раз в минуту значило бы
платить полным прогоном за ответ «ничего не изменилось».

Считает изменения `MutationObserver` **внутри страницы**, и это единственный
способ: из Python видно только результат, а не факт изменения — чтобы заметить
новую строку опросом, её пришлось бы сравнивать с прошлым снимком, гоняя всю
разметку через мост на каждой проверке.

Записи копятся на стороне Python, а не в странице: страница живёт до первой
навигации, а наблюдение должно её пережить — за тем оно и заводится.

⚠ **Изменения приходят пачками, а не по одному.** Один клик по чужому сайту
рождает десятки мутаций (перерисовка, анимация, вставка обёрток), а живой чат —
сотни в минуту. Пачка раз в `BROWSER_WATCH_FLUSH_MS` вместо сообщения на каждую
мутацию — разница между работающим наблюдателем и мостом, забитым служебным
трафиком.
"""

import asyncio
import time

from logging_.logging_ import logger_info

from browser_.browser_pool import browser_pool_pages
from browser_.browser_selector import BROWSER_SELECTOR_ONE_JS

BROWSER_WATCH_BINDING = '__browserWatchEmit'

# Ключ в `sessionStorage` страницы: в нём лежит настройка наблюдения. Нужен,
# чтобы наблюдение пережило переход по ссылке — init-скрипт нового документа
# прочитает его и включится сам, не дожидаясь команды из Python.
BROWSER_WATCH_KEY = '__browserWatch'

# Сколько записей держим. Наблюдатель висит сутками, и без предела он съел бы
# память контейнера — а нужны почти всегда последние: «что нового», а не «что
# было во вторник».
BROWSER_WATCH_LIMIT = 500

# Как часто страница отдаёт накопленное. Треть секунды: человек не заметит
# задержки, а пачка вместо сотни сообщений экономит мост.
BROWSER_WATCH_FLUSH_MS = 300

# Сколько знаков текста берём с изменившегося узла. Пришедшее сообщение чата
# укладывается в это с запасом, а вставленная реклама на полстраницы — нет, и не
# должна.
BROWSER_WATCH_TEXT_LIMIT = 500

# Сколько изменений отдаём одной пачкой. Перерисовка списка целиком — это тысяча
# мутаций подряд, и все они об одном.
BROWSER_WATCH_BATCH_LIMIT = 50

# Ставится на каждый документ сессии — включая переходы и дочерние окна.
BROWSER_WATCH_JS = """
(() => {
  // Флаг на документе, а не на окне: у всплывшего окна `window` переживает
  // навигацию, а `document` — нет, и наблюдатель остался бы на прошлом документе.
  if (document.__browserWatchOn) return;
  document.__browserWatchOn = true;

  const selectorOf = __SELECTOR_ONE__;
  const KEY = '__KEY__';
  const FLUSH = __FLUSH__;
  const TEXT_LIMIT = __TEXT_LIMIT__;
  const BATCH_LIMIT = __BATCH_LIMIT__;

  let observer = null;
  let batch = [];
  let timer = null;
  let dropped = 0;

  const textOf = (node) => {
    const raw = node.nodeType === 3 ? (node.textContent || '')
                                    : (node.innerText || node.textContent || '');
    return raw.replace(/\\s+/g, ' ').trim().slice(0, TEXT_LIMIT);
  };

  const flush = () => {
    timer = null;
    if (!batch.length) return;
    const rows = batch;
    batch = [];
    try { window.__browserWatchEmit({rows: rows, dropped: dropped}); } catch (e) {}
    dropped = 0;
  };

  const add = (row) => {
    if (batch.length >= BATCH_LIMIT) { dropped += 1; return; }
    batch.push(row);
    if (timer === null) timer = setTimeout(flush, FLUSH);
  };

  // Элемент, к которому имеет смысл считать селектор. У текстового узла своего
  // селектора нет — берём родителя: именно он и есть «место, где изменилось».
  const holder = (node) => (node.nodeType === 1 ? node : (node.parentElement || null));

  // ⚠ У пришедшего сообщения чата **устойчивого селектора обычно нет**, и это
  // норма, а не сбой: строк с классом `msg` на странице десяток, и ни одна не
  // адресуется однозначно. Наблюдателю уникальность и не нужна — вопрос здесь
  // «где изменилось», а не «за что нажать», — поэтому пустой селектор заменяется
  // приметой места: тег с парой классов. Пустая строка в записи не говорила бы
  // ничего вовсе.
  const describe = (el) => {
    if (!el || !el.tagName) return '';
    const exact = selectorOf(el);
    if (exact) return exact;
    const cls = (el.getAttribute('class') || '').split(/\\s+/).filter(Boolean).slice(0, 2);
    return el.tagName.toLowerCase() + (cls.length ? '.' + cls.join('.') : '');
  };

  const onRecords = (records) => {
    for (const record of records) {
      if (record.type === 'childList') {
        for (const node of record.addedNodes) {
          const text = textOf(node);
          // Пустая вставка — служебная обёртка, распорка, комментарий: сообщать о
          // ней нечего. Наблюдают за содержимым, а не за деревом как таковым.
          if (!text) continue;
          add({kind: 'added', selector: describe(holder(node)), text: text});
        }
        for (const node of record.removedNodes) {
          const text = textOf(node);
          if (!text) continue;
          add({kind: 'removed', selector: describe(holder(record.target)), text: text});
        }
      } else if (record.type === 'characterData') {
        const text = textOf(record.target);
        if (text) add({kind: 'text', selector: describe(holder(record.target)), text: text});
      } else if (record.type === 'attributes') {
        const el = record.target;
        add({kind: 'attr', selector: describe(el),
             text: record.attributeName + '=' + ((el.getAttribute(record.attributeName) || '')
                                                 .slice(0, TEXT_LIMIT))});
      }
    }
  };

  const start = (opts) => {
    stop();
    const options = opts || {};
    const root = document.querySelector(options.selector || 'body') || document.body;
    if (!root) return false;

    observer = new MutationObserver(onRecords);
    observer.observe(root, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: !!options.attributes,
    });
    try { sessionStorage.setItem(KEY, JSON.stringify(options)); } catch (e) {}
    return true;
  };

  const stop = () => {
    if (observer) { observer.disconnect(); observer = null; }
    if (timer !== null) { clearTimeout(timer); timer = null; }
    batch = [];
    try { sessionStorage.removeItem(KEY); } catch (e) {}
  };

  window.__browserWatch = {start, stop, live: () => !!observer};

  // Переход по ссылке наблюдение не гасит: настройка лежит в `sessionStorage`, и
  // новый документ включает её сам. Ради этого наблюдатель и заводят — смотреть
  // за страницей, по которой ходят.
  //
  // ⚠ **Только после разбора разметки.** Init-скрипт достаётся документу раньше
  // его собственных тегов: на этот момент ни `#chat`, ни даже `body` ещё нет, и
  // `observe` было некому — наблюдение молча не переживало перезагрузку (поймано
  // живьём: после `reload` записи переставали приходить вовсе). Отсюда ожидание
  // `DOMContentLoaded`; цена — изменения, случившиеся до конца разбора разметки,
  // в записи не попадут, но за ними и не наблюдают.
  const autostart = () => {
    try {
      const saved = sessionStorage.getItem(KEY);
      if (saved) start(JSON.parse(saved));
    } catch (e) {}
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autostart, {once: true});
  } else {
    autostart();
  }
})();
""".replace('__SELECTOR_ONE__', BROWSER_SELECTOR_ONE_JS
            ).replace('__KEY__', BROWSER_WATCH_KEY
            ).replace('__FLUSH__', str(BROWSER_WATCH_FLUSH_MS)
            ).replace('__TEXT_LIMIT__', str(BROWSER_WATCH_TEXT_LIMIT)
            ).replace('__BATCH_LIMIT__', str(BROWSER_WATCH_BATCH_LIMIT))


async def browser_watch_start(entry: dict, selector: str = 'body', attributes: bool = False,
                              limit: int = BROWSER_WATCH_LIMIT) -> dict:
    """
    Начинает наблюдение за поддеревом страницы во всей сессии.

    Args:
        entry: запись сессии из пула.
        selector: корень наблюдения. `body` — вся страница; сузить — главный способ
            избавиться от шума: за списком сообщений (`.chat-list`) наблюдать
            осмысленно, за всей страницей вместе с её анимацией — почти нет.
        attributes: считать и правку атрибутов. По умолчанию нет: атрибуты меняются
            на каждое движение мыши (`class="hover"`), и полезное в этом потоке
            теряется.
        limit: сколько записей держать.

    Returns:
        dict: состояние наблюдения (`browser_watch_info` от него же).

    ⚠ **Повторный вызов — это «наблюдай заново», а не «наблюдай вдвое»**: прежний
    наблюдатель снимается, накопленное обнуляется. Иначе записи прошлого
    наблюдения смешались бы с новым, и разобрать, что чему принадлежит, было бы
    нечем — та же причина, что у второго `net_watch` в сценарии.
    """
    await _install(entry)

    entry['watch'] = {'rows': [], 'selector': selector or 'body',
                      'attributes': bool(attributes), 'limit': int(limit),
                      'started_at': time.time(), 'dropped': 0, 'live': True}

    options = {'selector': selector or 'body', 'attributes': bool(attributes)}
    for page in browser_pool_pages(entry):
        await _page_start(page, options)

    logger_info(f"[browser] наблюдатель DOM включён: {entry['session_id']}, корень «{selector}»")

    return browser_watch_info(entry.get('watch'))


async def browser_watch_stop(entry: dict) -> list:
    """
    Гасит наблюдение и отдаёт накопленное.

    Записи возвращаются, а не выбрасываются: наблюдение обычно и гасят потому, что
    дождались того, чего ждали, — и последняя пачка нужна как раз в этот момент.
    """
    state = entry.pop('watch', None)

    for page in browser_pool_pages(entry):
        await _page_stop(page)

    if state is None:
        return []

    state['live'] = False
    logger_info(f"[browser] наблюдатель DOM выключен: {entry['session_id']}, "
                f"записей {len(state['rows'])}")

    return state['rows']


def browser_watch_take(entry: dict, clear: bool = True) -> list:
    """
    Забирает накопленное, не гася наблюдение.

    `clear` по умолчанию **истина**, и это осознанно: спрашивают «что нового», а не
    «что было всего». Оставлять забранное значило бы отдавать одно и то же
    сообщение чата при каждом опросе, а отличить его от настоящего повтора
    вызывающему нечем.
    """
    state = entry.get('watch')
    if not state:
        return []

    rows = state['rows']
    if clear:
        state['rows'] = []
        state['dropped'] = 0

    return rows


def browser_watch_info(state: dict | None) -> dict:
    """Сводка: идёт ли наблюдение, за чем, сколько записей и сколько потеряно."""
    if not state:
        return {'live': False, 'rows': 0, 'dropped': 0, 'selector': '', 'uptime_sec': 0}

    return {
        'live': bool(state.get('live')),
        'selector': state.get('selector', ''),
        'attributes': bool(state.get('attributes')),
        'rows': len(state.get('rows') or []),
        'dropped': int(state.get('dropped') or 0),
        'uptime_sec': round(time.time() - float(state.get('started_at') or time.time())),
    }


async def _install(entry: dict) -> None:
    """Привязка, init-скрипт и подписка на новые окна — один раз на сессию.

    Ни привязку, ни init-скрипт Playwright снять не умеет, поэтому ставятся они
    однажды, а «наблюдаем или нет» решает состояние: скрипт молчит, пока его не
    попросили. Устройство то же, что у записи действий и инспектора.
    """
    if entry.get('watch_installed'):
        return

    context = entry['context']

    def emit(source, payload):
        _batch_put(entry, payload)

    await context.expose_binding(BROWSER_WATCH_BINDING, emit)
    await context.add_init_script(BROWSER_WATCH_JS)

    # Всплывшее окно получит init-скрипт само, но настройку наблюдения — нет: у
    # окна чужого origin своё `sessionStorage`, и от родителя оно ничего не
    # наследует. Подписка догоняет такое окно и включает наблюдение явно.
    def on_page(page):
        state = entry.get('watch')
        if state and state.get('live'):
            asyncio.create_task(_page_arm(page, state))

    context.on('page', on_page)
    entry['watch_installed'] = True


async def _page_arm(page, state: dict) -> None:
    """Догоняет всплывшее окно: ждёт документ и включает в нём наблюдение."""
    try:
        await page.wait_for_load_state('domcontentloaded')
    except Exception:
        pass

    await _page_start(page, {'selector': state.get('selector', 'body'),
                             'attributes': bool(state.get('attributes'))})


async def _page_start(page, options: dict) -> None:
    """Включает наблюдение на одной странице. Уже открытые init-скрипта не
    получали — им он ставится тем же `evaluate`, что и команда на запуск."""
    try:
        if page.is_closed():
            return
        await page.evaluate(BROWSER_WATCH_JS)
        await page.evaluate(
            '(opts) => { const w = window.__browserWatch; return w ? w.start(opts) : false; }',
            options)
    except Exception as e:
        logger_info(f'[browser] наблюдатель: страница не включилась: {e}')


async def _page_stop(page) -> None:
    """Гасит наблюдение на одной странице. Закрылась по дороге — не наша забота."""
    try:
        if not page.is_closed():
            await page.evaluate('() => { const w = window.__browserWatch; if (w) w.stop(); }')
    except Exception as e:
        logger_info(f'[browser] наблюдатель: страница не выключилась: {e}')


def _batch_put(entry: dict, payload) -> None:
    """Пачка изменений со страницы → записи сессии.

    Приходит она из привязки, то есть из потока Playwright: исключение здесь
    уронило бы вызов внутри страницы, а не наш обход, поэтому разбор защищён
    целиком. Сессии к этому моменту может уже не быть — страница живёт своей
    жизнью и досылает последнюю пачку после того, как наблюдение погасили.
    """
    state = entry.get('watch')
    if not state:
        return

    try:
        data = payload if isinstance(payload, dict) else {}
        rows = data.get('rows') or []
        state['dropped'] += int(data.get('dropped') or 0)

        at = time.strftime('%H:%M:%S')
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if len(state['rows']) >= state['limit']:
                state['rows'].pop(0)
                state['dropped'] += 1
            state['rows'].append({
                'kind': str(row.get('kind') or '')[:20],
                'selector': str(row.get('selector') or '')[:500],
                'text': str(row.get('text') or '')[:BROWSER_WATCH_TEXT_LIMIT],
                'at': at,
            })
    except Exception as e:
        logger_info(f'[browser] наблюдатель: пачка не разобралась: {e}')

"""Инспектор элементов живого окна: подсветка под курсором и выбор кликом.

Окно показывает картинку, а не DOM, поэтому «навести и посмотреть, что это»
на стороне админки сделать нечем — там пиксели. Разметку знает только сама
страница, значит и подсветка, и выбор живут в ней: обводка рисуется своим
элементом поверх содержимого, клик перехватывается и наверх уходит пара
«селектор + разметка».

Устройство то же, что у записи (`browser_record`): `add_init_script` достаётся
каждому новому документу, а уже открытые страницы вооружаются отдельным
`evaluate` — режим включают на том, что перед глазами, а не со следующего
перехода. Снять init-скрипт Playwright не умеет, поэтому ставится он один раз
на сессию.

**Страниц у сессии больше одной.** Сайт открывает дочерние окна, живое окно само
переходит на всплывшее, и человек смотрит уже на него — поэтому режим включается
во всех страницах контекста, а подписка на `page` догоняет те, что всплыли позже.
`sessionStorage` держит включённость внутри страницы (режим переживает переход по
ссылке), но полагаться только на него нельзя: у окна чужого origin своё хранилище.

**Вариантов селектора несколько, потому что «правильный» зависит от того, что
переживёт правку вёрстки, — а это решает человек.** Устойчивый (по пометке
элемента) считается тем же кодом, что у снимка и записи (`browser_selector`):
показать человеку один селектор, а в сценарий записать другой — верный способ
чинить сценарий вслепую. Остальные варианты — текст, край списка, путь через
предков — считает страница рядом.

Выбранное уходит в очередь записи сессии, а не прямо в сокет: окно может быть
закрыто или переоткрыто, и знать о нём инспектору незачем.
"""

import asyncio

from logging_.logging_ import logger_info

from browser_.browser_pool import browser_pool_pages
from browser_.browser_selector import BROWSER_SELECTOR_ONE_JS

BROWSER_INSPECT_BINDING = '__browserInspectPick'

# Разметку показываем компактно: попап в углу окна, а не редактор. Целиком
# страницу отдавать и незачем — по началу тега всё и так понятно.
BROWSER_INSPECT_HTML_LIMIT = 2000
BROWSER_INSPECT_SELECTOR_LIMIT = 500

# Сколько вариантов селектора показываем. Шесть — это уже весь набор приёмов
# (пометка, текст, край списка, путь); больше значило бы предлагать выбор из
# вариантов, отличающихся ничем.
BROWSER_INSPECT_SELECTOR_VARIANTS = 6

# Ставится на каждый документ сессии — включая переходы и дочерние окна.
BROWSER_INSPECT_JS = """
(() => {
  // Флаг на документе, а не на окне: у всплывшего окна `window` переживает
  // навигацию, а `document` — нет, и слушатели остались бы на предыдущем
  // документе. Само API держим на `window` — его зовут снаружи, из Python.
  if (document.__browserInspectOn) return;
  document.__browserInspectOn = true;

  const selectorOf = __SELECTOR_ONE__;
  const KEY = '__browserInspect';

  // Свои `esc` и `quote`: одноимённые живут внутри `selectorOf` и снаружи не видны —
  // без этих строк сбор вариантов падал бы с ReferenceError, а `onClick` глотал бы
  // ошибку и молчал.
  const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v).replace(/(["\\\\])/g, '\\\\$1');
  const quote = (v) => '"' + String(v).replace(/(["\\\\])/g, '\\\\$1') + '"';

  // Обводка — свой элемент поверх страницы, а не `outline` на самом элементе:
  // чужой `outline` меняет вёрстку, а на элементе с `overflow: hidden` его
  // попросту не видно.
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;display:none;'
    + 'border:2px solid #6366f1;background:rgba(99,102,241,.14);box-sizing:border-box';

  let current = null;

  const paint = (el) => {
    if (!el || !el.getBoundingClientRect) return;
    const r = el.getBoundingClientRect();
    box.style.display = '';
    box.style.left = r.left + 'px';
    box.style.top = r.top + 'px';
    box.style.width = r.width + 'px';
    box.style.height = r.height + 'px';
  };

  const onMove = (event) => {
    current = event.target;
    paint(current);
  };

  // Сколько элементов страницы совпадёт с селектором. -1 — CSS не разобрался
  // (например, у него псевдокласс Playwright, которого браузер не знает).
  const countOf = (css) => {
    try { return document.querySelectorAll(css).length; } catch (e) { return -1; }
  };

  // Короткое описание элемента для пути: тег и пара «смысловых» классов.
  // Классы состояния и с цифрами выброшены по той же причине, что и в основном
  // селекторе: `active` слетит при переключении, `col-3` — при правке сетки.
  const STATE = ['active', 'selected', 'current', 'open', 'show', 'hidden', 'disabled', 'checked', 'error'];
  const simpleSel = (el) => {
    const tag = el.tagName.toLowerCase();
    if (/^[A-Za-z][\\w-]*$/.test(el.id || '')) return '#' + esc(el.id);
    const cls = (el.getAttribute('class') || '').split(/\\s+/)
      .filter((c) => c && !/\\d/.test(c) && STATE.indexOf(c.toLowerCase()) < 0)
      .slice(0, 2);
    return cls.length ? tag + '.' + cls.map(esc).join('.') : tag;
  };

  // Собственный текст элемента: он и виден человеку, и переживает правку вёрстки
  // лучше любого пути. Берём только короткий — по абзацу селектор не пишут.
  //
  // И только у элемента без своей вёрстки внутри: у `div`-обёртки текст — это
  // текст всех её потомков разом, и `:has-text` по нему совпадёт с половиной
  // страницы. Одно-два вложенных допускаем: `<li><span>Logout</span></li>` —
  // обычная разметка, и текст там по-прежнему про сам элемент.
  const ownText = (el) => {
    if (el.children && el.children.length > 2) return '';
    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
    return text && text.length <= 40 ? text : '';
  };

  const selectorsOf = (el) => {
    const out = [];
    const add = (label, value, count) => {
      if (!value) return;
      if (out.some((row) => row.value === value)) return;
      out.push({label: label, value: value, count: count === undefined ? countOf(value) : count});
    };

    const tag = el.tagName.toLowerCase();
    const parent = el.parentElement;

    add('устойчивый', selectorOf(el));

    // По тексту: Playwright понимает `:has-text`, браузер — нет, поэтому число
    // совпадений считаем сами, перебрав элементы того же тега.
    const text = ownText(el);
    if (text) {
      const same = Array.prototype.filter.call(
        document.getElementsByTagName(tag), (node) => (node.textContent || '').indexOf(text) >= 0);
      add('по тексту', tag + ':has-text(' + quote(text) + ')', same.length);
    }

    if (parent) {
      const box = simpleSel(parent);
      const kin = Array.prototype.filter.call(parent.children, (node) => node.tagName === el.tagName);
      const index = kin.indexOf(el);

      // Позиция среди соседей — только края: «третий» ломается от любой вставки
      // выше, а «первый» и «последний» переживают добавление в середину списка.
      if (kin.length > 1 && index === 0) add('первый в родителе', box + ' > ' + tag + ':first-child');
      if (kin.length > 1 && index === kin.length - 1) {
        add('последний в родителе', box + ' > ' + tag + ':last-child');
        // `nth=-1` считает от всех совпадений, а не от одного родителя: у списка,
        // разбитого на несколько `ul`, это единственный способ взять последний.
        add('последний из всех', box + ' ' + tag + ' >> nth=-1', 1);
      }

      // Путь через двух предков: когда сам элемент ничем не помечен, отличить его
      // можно только тем, где он лежит.
      const grand = parent.parentElement;
      if (grand && grand !== document.documentElement) {
        add('через двух предков', simpleSel(grand) + ' > ' + box + ' > ' + tag);
      }
      add('в родителе', box + ' ' + tag);
    }

    return out.slice(0, __LIMIT__);
  };

  // Клик в режиме инспектора смотрит, а не нажимает: иначе выбор ссылки уводил
  // бы страницу раньше, чем человек прочитал её селектор.
  const onClick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    const el = event.target;
    try {
      window.__browserInspectPick({
        selector: selectorOf(el) || '',
        selectors: selectorsOf(el),
        html: (el.outerHTML || '').slice(0, __HTML_LIMIT__),
        tag: (el.tagName || '').toLowerCase(),
      });
    } catch (e) {}
  };

  const on = () => {
    (document.documentElement || document.body).appendChild(box);
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('click', onClick, true);
    try { sessionStorage.setItem(KEY, '1'); } catch (e) {}
  };

  const off = () => {
    box.remove();
    box.style.display = 'none';
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('click', onClick, true);
    try { sessionStorage.removeItem(KEY); } catch (e) {}
  };

  window.__browserInspect = {on, off};

  // Режим включают, чтобы ходить по страницам и смотреть: переход его не гасит.
  try { if (sessionStorage.getItem(KEY) === '1') on(); } catch (e) {}
})();
""".replace('__SELECTOR_ONE__', BROWSER_SELECTOR_ONE_JS
            ).replace('__HTML_LIMIT__', str(BROWSER_INSPECT_HTML_LIMIT)
            ).replace('__LIMIT__', str(BROWSER_INSPECT_SELECTOR_VARIANTS))


async def browser_inspect_set(entry: dict, on: bool) -> None:
    """Включает или гасит инспектор во всех страницах сессии.

    Именно во всех, а не только в первой: сайт открывает дочерние окна, живое окно
    само переходит на всплывшее (`browser_view`), и человек смотрит уже на него.
    Инспектор, включённый на одной странице сессии, в таком окне не срабатывал.

    Флаг держит Python (`entry['inspect']`), а не только `sessionStorage` страницы:
    у окна чужого origin своё хранилище, и унаследовать его от родителя оно не может.
    Поэтому новые страницы включает подписка внутри `_install`.
    """
    await _install(entry)

    entry['inspect'] = bool(on)
    for page in browser_pool_pages(entry):
        await _page_switch(page, bool(on))

    logger_info(f"[browser] инспектор {'включён' if on else 'выключен'}: {entry['session_id']}")


def browser_inspect_queue(entry: dict) -> asyncio.Queue:
    """Очередь выбранных элементов сессии — из неё читает живое окно."""
    queue = entry.get('inspect_queue')
    if queue is None:
        queue = asyncio.Queue(maxsize=8)
        entry['inspect_queue'] = queue

    return queue


async def _page_switch(page, on: bool) -> None:
    """Переключает режим на одной странице. Закрылась по дороге — не наша забота."""
    try:
        await page.evaluate(
            '(on) => { const i = window.__browserInspect; if (!i) return; on ? i.on() : i.off(); }',
            on)
    except Exception as e:
        logger_info(f'[browser] инспектор: страница не переключилась: {e}')


async def _install(entry: dict) -> None:
    """Привязка, init-скрипт и подписка на новые окна — один раз на сессию."""
    if entry.get('inspect_installed'):
        return

    context = entry['context']

    def pick(source, payload):
        _pick_put(entry, payload)

    await context.expose_binding(BROWSER_INSPECT_BINDING, pick)
    await context.add_init_script(BROWSER_INSPECT_JS)

    # Всплывшее окно получит init-скрипт само, но включённый режим — нет: он живёт
    # флагом в Python. Подписка догоняет новую страницу и включает в ней инспектор,
    # если он сейчас включён; снять её нечем, поэтому ставится один раз на сессию.
    def on_page(page):
        if entry.get('inspect'):
            asyncio.create_task(_page_arm(page, entry))

    context.on('page', on_page)

    # Уже открытые страницы init-скрипта не получали: вооружаем каждую отдельно,
    # иначе инспектор заработал бы только со следующего перехода.
    for page in browser_pool_pages(entry):
        await _page_evaluate(page, BROWSER_INSPECT_JS)

    entry['inspect_installed'] = True


async def _page_arm(page, entry: dict) -> None:
    """Догоняет всплывшее окно: ждёт загрузки и включает в нём режим."""
    try:
        await page.wait_for_load_state('domcontentloaded')
    except Exception:
        pass

    await _page_evaluate(page, BROWSER_INSPECT_JS)
    await _page_switch(page, bool(entry.get('inspect')))


async def _page_evaluate(page, script: str) -> None:
    """Выполняет скрипт на странице, не роняя вызывающего."""
    try:
        if not page.is_closed():
            await page.evaluate(script)
    except Exception as e:
        logger_info(f'[browser] инспектор: скрипт не встал на страницу: {e}')


def _variants(raw) -> list:
    """Варианты селектора со страницы: обрезаем и приводим к одной форме.

    `count` — сколько элементов совпало: 1 годится для клика, больше — шаг упадёт
    на строгом `page.locator`, а -1 значит «браузер этот селектор не разбирает»
    (псевдоклассы Playwright он и не должен понимать).
    """
    out = []
    for item in (raw if isinstance(raw, list) else [])[:BROWSER_INSPECT_SELECTOR_VARIANTS]:
        if not isinstance(item, dict):
            continue
        value = str(item.get('value') or '').strip()[:BROWSER_INSPECT_SELECTOR_LIMIT]
        if not value:
            continue
        out.append({'label': str(item.get('label') or '')[:40],
                    'value': value,
                    'count': int(item.get('count') if str(item.get('count') or '').lstrip('-').isdigit() else -1)})

    return out


def _pick_put(entry: dict, payload) -> None:
    """Кладёт выбранное в очередь. Полная очередь — выбрасываем старое.

    Смотрят последний ткнутый элемент, а не всю историю тычков: попап показывает
    один, и придержанные кадры выбора только отстали бы от курсора.
    """
    queue = browser_inspect_queue(entry)
    data = payload if isinstance(payload, dict) else {}
    message = {
        'type': 'inspect',
        'selector': str(data.get('selector') or '')[:BROWSER_INSPECT_SELECTOR_LIMIT],
        'selectors': _variants(data.get('selectors')),
        'html': str(data.get('html') or '')[:BROWSER_INSPECT_HTML_LIMIT],
        'tag': str(data.get('tag') or '')[:50],
    }

    while queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    queue.put_nowait(message)

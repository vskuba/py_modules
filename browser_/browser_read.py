"""Читаемый текст страницы: то, что видит человек, — в том виде, в каком его поймёт LLM.

Три способа отдать страницу модели, и два из них плохи. **HTML** — это сотни
килобайт разметки, из которых значимы полтора десятка строк; в запрос он не влезает,
а влезет — половина внимания модели уйдёт на классы и `<div>`. **Снимок дерева
доступности** (`browser_snapshot`) отвечает на другой вопрос: там роли, подписи и
селекторы, то есть «за что тут можно нажать», а не «что здесь написано» — по нему не
прочитать ни письмо, ни объявление, ни статью.

Отсюда третий: **видимый текст с сохранённой структурой.** Заголовки остаются
заголовками (`##`), пункты списка — пунктами (`- `), поля формы показывают своё
значение. Невидимое выброшено: `display: none`, `aria-hidden`, скрипты и стили. Это
дешевле HTML в десятки раз и читается моделью без пояснений — то же, что она видит в
обычном сообщении.

⚠ **Скрытое выброшено намеренно, и это не мелочь.** Страница держит в разметке
шаблоны будущих сообщений, заготовки модальных окон и меню, которых на экране нет.
Попади они в текст, модель отвечала бы на то, чего человек не видел.
"""

from logging_.logging_ import logger_info

# Сколько знаков текста отдаём. Двадцать тысяч — это ~5 тысяч токенов: столько
# страница стоит в запросе к модели, и больше в разумный запрос не влезает. Обрезка
# идёт сверху вниз, и текст остаётся осмысленным: начало страницы почти всегда
# важнее её подвала.
BROWSER_READ_TEXT_LIMIT = 20000

# Сколько ссылок отдаём. Ссылки нужны, чтобы модель могла сказать «иди сюда»; в
# подвале их бывают сотни, и ценность у них падает с первого десятка.
BROWSER_READ_LINK_LIMIT = 200

BROWSER_READ_LINK_TEXT_LIMIT = 120

# Разбор идёт **внутри страницы**, а не над её HTML в Python: видимость элемента
# знает только браузер (`getComputedStyle`), и в разметке её нет ни в каком виде.
# Обход вручную, а не `innerText`, ради структуры: `innerText` отдаёт плоскую
# простыню, в которой заголовок неотличим от абзаца, а поле формы — от подписи.
BROWSER_READ_JS = """
([selector, limit, wantLinks, linkLimit, linkTextLimit]) => {
  const root = document.querySelector(selector) || document.body;
  if (!root) return {text: '', links: [], truncated: false};

  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME', 'CANVAS',
                        'SVG', 'VIDEO', 'AUDIO', 'OBJECT', 'EMBED', 'MAP']);
  const HEAD = {H1: '# ', H2: '## ', H3: '### ', H4: '#### ', H5: '##### ', H6: '###### '};
  const BLOCK = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DD', 'DETAILS',
                         'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION', 'FIGURE', 'FOOTER',
                         'FORM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER', 'LI',
                         'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'SUMMARY', 'TABLE',
                         'TD', 'TH', 'TR', 'UL', 'LABEL', 'BUTTON', 'OPTION']);

  const parts = [];
  let chars = 0;
  let truncated = false;

  const push = (text) => {
    if (!text) return;
    if (chars >= limit) { truncated = true; return; }
    parts.push(text);
    chars += text.length;
  };
  const newline = () => {
    if (parts.length && parts[parts.length - 1] !== '\\n') parts.push('\\n');
  };

  const invisible = (el) => {
    if (el.getAttribute('aria-hidden') === 'true') return true;
    const style = window.getComputedStyle(el);
    return style.display === 'none' || style.visibility === 'hidden';
  };

  // Поле формы показывает своё значение: без него страница профиля выглядит
  // набором подписей без ответов, а именно ответы и спрашивают.
  const fieldText = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const label = (el.getAttribute('placeholder') || el.getAttribute('aria-label')
                   || el.getAttribute('name') || type || tag).trim();
    if (type === 'checkbox' || type === 'radio') {
      return '[' + label + ': ' + (el.checked ? 'да' : 'нет') + ']';
    }
    // Пароль не показываем даже здесь: текст страницы уезжает в запрос к модели и
    // в журнал, а введённое в поле пароля туда попасть не должно ни разу.
    if (type === 'password') return '[' + label + ': ***]';
    const value = el.value == null ? '' : String(el.value).slice(0, 200);
    return '[' + label + (value ? ': ' + value : ': пусто') + ']';
  };

  const walk = (node) => {
    if (truncated) return;
    if (node.nodeType === 3) {
      push((node.textContent || '').replace(/\\s+/g, ' '));
      return;
    }
    if (node.nodeType !== 1) return;

    const tag = node.tagName;
    if (SKIP.has(tag) || invisible(node)) return;

    if (tag === 'BR' || tag === 'HR') { newline(); return; }
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      push(' ' + fieldText(node) + ' ');
      return;
    }
    if (tag === 'IMG') {
      const alt = (node.getAttribute('alt') || '').trim();
      if (alt) push(' ![' + alt + '] ');
      return;
    }

    const block = BLOCK.has(tag);
    if (block) newline();
    if (HEAD[tag]) push(HEAD[tag]);
    if (tag === 'LI') push('- ');

    for (const child of node.childNodes) walk(child);
    if (block) newline();
  };

  walk(root);

  const links = [];
  if (wantLinks) {
    const seen = new Set();
    for (const a of root.querySelectorAll('a[href]')) {
      if (links.length >= linkLimit) break;
      const href = a.href || '';
      if (!href || href.startsWith('javascript:') || seen.has(href)) continue;
      if (invisible(a)) continue;
      seen.add(href);
      const text = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
      links.push({text: text.slice(0, linkTextLimit), href: href});
    }
  }

  return {text: parts.join(''), links: links, truncated: truncated};
}
"""


async def browser_read_page(page, selector: str = 'body', links: bool = False,
                            limit: int = BROWSER_READ_TEXT_LIMIT) -> dict:
    """
    Видимый текст страницы (или её части) с сохранённой структурой.

    Args:
        page: страница Playwright.
        selector: корень чтения. `body` — вся страница; `main`, `.chat` — её часть,
            и это главный способ сократить текст осмысленно, а не обрезкой по счёту
            знаков: подвал сайта модели не нужен ни в каком объёме.
        links: собрать заодно ссылки — «куда с этой страницы можно уйти».
        limit: предел на знаки текста.

    Returns:
        dict: `title`, `url`, `text`, `chars`, `truncated`, `links`.

    ⚠ **Сбой чтения — пустой текст, а не исключение.** Страница может уйти на
    другую прямо во время обхода (`Execution context was destroyed`), и ронять из-за
    этого весь ответ незачем: причина уезжает в `error`, а вызывающий решает,
    повторять ли.
    """
    try:
        result = await page.evaluate(BROWSER_READ_JS,
                                     [selector or 'body', int(limit), bool(links),
                                      BROWSER_READ_LINK_LIMIT, BROWSER_READ_LINK_TEXT_LIMIT])
    except Exception as e:
        logger_info(f'[browser] текст страницы не прочитался: {str(e).splitlines()[0]}')
        return {'title': '', 'url': _url(page), 'text': '', 'chars': 0,
                'truncated': False, 'links': [], 'error': str(e).splitlines()[0][:200]}

    text = browser_read_squeeze(str(result.get('text') or ''))[:limit]

    return {
        'title': await _title(page),
        'url': _url(page),
        'text': text,
        'chars': len(text),
        'truncated': bool(result.get('truncated')) or len(text) >= limit,
        'links': result.get('links') or [],
        'error': '',
    }


def browser_read_squeeze(text: str) -> str:
    """
    Приводит собранный текст к читаемому виду: пустые строки, отступы, повторы.

    Обход даёт много пустоты: каждая `div`-обёртка просит перевод строки, и на
    типичной вёрстке их по десятку на абзац. Отдельно выбрасываются **подряд идущие
    одинаковые строки** — так выглядит меню, отрисованное дважды (для широкого
    экрана и для узкого), и модель, увидев его дважды, честно решает, что на
    странице два меню.
    """
    lines = []
    for raw in str(text or '').split('\n'):
        line = ' '.join(raw.split())
        if not line:
            # Пустые строки не копим: одна разделяет абзацы, десять ничего не
            # добавляют, а места в запросе к модели стоят столько же.
            if lines and lines[-1] != '':
                lines.append('')
            continue
        # Одинокий маркер пункта остаётся от списка, у которого текст в дочернем
        # блоке: строка «- » ниже своего же содержимого сбивает с толку.
        if line == '-':
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)

    return '\n'.join(lines).strip()


def _url(page) -> str:
    """Адрес страницы. Свойство, а не запрос в браузер: занятая своим JavaScript
    страница не должна задерживать ответ."""
    try:
        return page.url
    except Exception:
        return ''


async def _title(page) -> str:
    """Заголовок страницы. В отличие от адреса это вызов в страницу, поэтому и
    неудача здесь обычное дело: пустая строка честнее исключения."""
    try:
        return await page.title()
    except Exception:
        return ''

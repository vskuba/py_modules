"""Устойчивый селектор элемента — один алгоритм на три потребителя.

Селектор для шага сценария считают три разных места: снимок страницы для LLM
(`browser_snapshot`), запись действий за человеком (`browser_record`) и инспектор
живого окна (`browser_inspect`). Показать человеку один селектор, записать в
сценарий другой, а модели предложить третий — верный способ чинить сценарий
вслепую: падает шаг, написанный одним способом, а глазами смотрят на то, что
посчитано другим.

Поэтому алгоритм здесь один, строкой JavaScript, и выполняется он **внутри
страницы**. В Python его не перенести: проверка «сколько элементов совпало с этим
селектором» — это `document.querySelectorAll`, и каждый кандидат стоил бы
отдельного обращения в браузер.

Порядок предпочтений — от самого долгоживущего к самому хрупкому: `id`,
`data-testid`, `name`, `aria-label`, `placeholder`, прочие `data-*`, класс без
цифр и без классов состояния, и лишь в конце — тег. Селектор с индексом
(`div:nth-child(3)`) не выдаётся никогда: он ломается от любой вставки выше по
дереву, а сценарий на нём молча начинает кликать не туда — это хуже честного
падения.
"""

# Селектор одного элемента: функция `(el) => 'css'`. Пустая строка — не нашлось
# ни одного кандидата, совпадающего ровно с одним элементом страницы.
#
# Строкой, а не файлом рядом: её вставляют внутрь других скриптов подстановкой
# (`browser_record`, `browser_inspect` собирают из неё свои слушатели), и отдельный
# файл пришлось бы читать с диска в момент, когда страница уже ждёт скрипт.
BROWSER_SELECTOR_ONE_JS = """
(el) => {
  const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v).replace(/(["\\\\])/g, '\\\\$1');
  const quote = (v) => '"' + String(v).replace(/(["\\\\])/g, '\\\\$1') + '"';
  const unique = (css) => { try { return document.querySelectorAll(css).length === 1; } catch (e) { return false; } };

  if (!el || !el.tagName) return '';
  const tag = el.tagName.toLowerCase();
  const attr = (n) => (el.getAttribute(n) || '').trim();
  const candidates = [];

  if (/^[A-Za-z][\\w-]*$/.test(attr('id'))) candidates.push('#' + esc(attr('id')));
  const DATA_KNOWN = ['data-testid', 'data-test', 'data-qa'];
  for (const name of DATA_KNOWN) {
    if (attr(name)) candidates.push('[' + name + '=' + quote(attr(name)) + ']');
  }
  if (attr('name')) candidates.push(tag + '[name=' + quote(attr('name')) + ']');
  if (attr('aria-label')) candidates.push(tag + '[aria-label=' + quote(attr('aria-label')) + ']');
  if (attr('placeholder')) candidates.push(tag + '[placeholder=' + quote(attr('placeholder')) + ']');
  if (tag === 'input' && attr('type')) candidates.push('input[type=' + quote(attr('type')) + ']');

  // Прочие `data-*` — раньше классов: разметка помечает ими смысл
  // (`data-tab="development"`), и такая пометка переживает правку стилей.
  // Числовые значения пропущены: `data-id="53"` уникален ровно до тех пор,
  // пока на странице та же строка данных.
  for (const a of Array.from(el.attributes || [])) {
    const n = a.name.toLowerCase();
    if (!n.startsWith('data-') || DATA_KNOWN.includes(n)) continue;
    const v = (a.value || '').trim();
    if (!v || v.length > 60 || /^\\d+$/.test(v)) continue;
    candidates.push(tag + '[' + n + '=' + quote(v) + ']');
  }

  // Классы состояния отброшены: `button.page-tab.active` перестанет совпадать,
  // как только вкладку переключат, — а элемент останется тем же самым.
  const STATE = ['active', 'selected', 'current', 'open', 'show', 'hidden', 'disabled', 'checked', 'error'];
  const classes = attr('class').split(/\\s+/)
    .filter((c) => c && !/\\d/.test(c) && !STATE.includes(c.toLowerCase()))
    .slice(0, 3);
  if (classes.length) candidates.push(tag + '.' + classes.map(esc).join('.'));
  candidates.push(tag);

  for (const css of candidates) { if (unique(css)) return css; }
  return '';
}
"""

# Тот же алгоритм пачкой: `(elements) => ['css', …]`. Нужен снимку страницы —
# сотня отдельных `evaluate` на элемент стоила бы сотни обращений к браузеру
# вместо одного.
BROWSER_SELECTOR_ALL_JS = (
    '(elements) => { const one = ' + BROWSER_SELECTOR_ONE_JS +
    '; return elements.map((el) => one(el)); }'
)

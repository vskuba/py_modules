"""
Страница как юнит: инлайн-скрипт HTML-страницы исполняется на node с заглушками
DOM/FileReader/Image, а тесты проверяют обработчики без браузера.

Бывает, что проверять надо логику обработчика (дефолты, нормализацию, проценты,
мосты `window.X_SET`), а не рендеринг: `web_drive` для этого тяжеловат и
медленен, а выковыривать скрипт из страницы в отдельный js-файл — удочка,
которая кривится при первой правке страницы. Здесь страница читается как есть:
её инлайн-скрипт (последний, шаблонный) дописывается тестами и исполняется с
заглушками ровно того объёма, каким живут обработчики.

Грабли, перепроверенные на обработчике загрузки QR:

- `FileReader` и `Image` обязаны стрелять колбэками асинхронно (`setTimeout`),
  иначе тесты проходят на синхронщине, которой в браузере нет, и валят
  настоящие же правки; тест после события делает `await tick()`.
- `form.elements` обязан быть одним реестром с `getElementById`: два входа к
  одному полю разъезжаются, и тест проверяет не страницу, а заглушку.
- узлы создаются на-demand: страница тянет `getElementById('что-угодно')`,
  заглушка не ноет, а даёт узел — как живой document для скрипта без опечаток.

node — системная зависимость (как ffmpeg в `adb_rec`): отсутствие — отдельная
ошибка, а не тихий пропуск.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile

# Потолок прогона тестов, секунды: зависший await в тестах не должен держать
# вызывающего дольше минуты.
WEB_UNIT_TIMEOUT = 60.0

# Маркеры, между которыми node печатает результат; всё до них — журнал тестов.
_WEB_UNIT_MARK = '__WEB_UNIT_JSON__'

# Каркас прогона: заглушки, затем скрипт страницы, затем тесты. Токены
# подставляются replace'ом (в JS свои {} и %, форматирование опасно).
_WEB_UNIT_STUB = r"""
'use strict';
// Узлы реестра: страница тянет getElementById чего угодно — узел находится,
// как в живом document для скрипта без опечаток.
const __els = new Map();
function mkEl(id, type) {
  const cls = new Set();
  return {
    id: id, value: '', textContent: '', checked: false, hidden: false,
    type: type || 'text', files: undefined,
    style: { setProperty(k, v) { this[k] = v; } },
    classList: { add: c => cls.add(c), remove: c => cls.delete(c),
                 contains: c => cls.has(c),
                 toggle: (c, on) => { on === undefined ? (cls.has(c) ? cls.delete(c) : cls.add(c))
                                : (on ? cls.add(c) : cls.delete(c)); return cls.has(c); } },
    _cls: cls, _ev: {},
    addEventListener(name, fn) { (this._ev[name] = this._ev[name] || []).push(fn); },
    setAttribute(k, v) { this[k] = v; }, getAttribute(k) { return this[k] === undefined ? null : this[k]; },
    removeAttribute(k) { delete this[k]; },
    appendChild() {}, removeChild() {}, focus() {}, blur() {}, reset() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    // form.elements — тот же реестр, что getElementById: поле одно на два входа.
    get elements() {
      return new Proxy({}, { get: (t, k) => {
        if (typeof k !== 'string') { return undefined; }
        if (!(k in t)) { t[k] = el(k); }
        return t[k];
      } });
    },
  };
}
function el(id) {
  const key = String(id);
  if (!__els.has(key)) __els.set(key, mkEl(key));
  return __els.get(key);
}
// Тип файлового input известен из HTML — подставляется в __FILE_IDS__.
for (const fid of __FILE_IDS__) { el(fid).type = 'file'; }
global.document = {
  getElementById: el,
  addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  createElement: (tag) => mkEl('__created__' + tag, tag),
  body: mkEl('__body__'),
};
const __store = new Map();
global.localStorage = {
  getItem: (k) => (__store.has(String(k)) ? __store.get(String(k)) : null),
  setItem: (k, v) => __store.set(String(k), String(v)),
  removeItem: (k) => __store.delete(String(k)),
};
class File {
  // Браузерная сигнатура — (parts, name, opts); двухаргументный вариант
  // (parts, {name,type}) тоже принимается, чтобы тесты не спотыкались.
  constructor(parts, name, opts) {
    if (name && typeof name === 'object' && opts === undefined) { opts = name; name = undefined; }
    this._parts = [].concat(parts || []);
    this.name = name || (opts && opts.name) || 'file';
    this.type = (opts && opts.type) || '';
  }
  get __bytes() {
    return Buffer.concat(this._parts.map(p => Buffer.isBuffer(p) ? p
      : (p instanceof ArrayBuffer ? Buffer.from(p) : Buffer.from(String(p)))));
  }
}
global.File = File;
class FileReader {
  readAsDataURL(f) {
    // Асинхронно, как в браузере: синхронщина прогоняет тесты, которых браузер не знает.
    setTimeout(() => {
      this.result = 'data:' + (f.type || 'application/octet-stream') + ';base64,' + f.__bytes.toString('base64');
      if (this.onload) this.onload();
    }, 0);
  }
  readAsText(f) {
    setTimeout(() => { this.result = f.__bytes.toString('utf-8'); if (this.onload) this.onload(); }, 0);
  }
}
global.FileReader = FileReader;
class Image {
  constructor() { this._src = ''; }
  set src(v) {
    this._src = v;
    setTimeout(() => {
      const size = global.__image_size ? global.__image_size(v) : [706, 706];
      if (size === null) { if (this.onerror) this.onerror(); }
      else {
        this.naturalWidth = size[0]; this.naturalHeight = size[1];
        if (this.onload) this.onload();
      }
    }, 0);
  }
  get src() { return this._src; }
}
global.Image = Image;
global.window = global;
// node ≥ 21 даёт navigator только геттером — не переприсваивать, а допустить
// родной; страницы-обработчики читают из него редко и только существование.
if (typeof global.navigator === 'undefined') { global.navigator = {}; }
global.location = { href: 'file://unit', reload() {} };
global.alert = () => {};
// --- хелперы тестов ---
const __results = [];
function t(name, cond) { __results.push({ name: name, ok: !!cond }); }
function fire(nodeOrId, type) {
  const n = typeof nodeOrId === 'string' ? el(nodeOrId) : nodeOrId;
  for (const fn of (n._ev[type] || [])) fn({ target: n, currentTarget: n, preventDefault() {} });
}
const tick = (ms = 5) => new Promise(r => setTimeout(r, ms));
function storage(key) { return global.localStorage.getItem(key); }
// --- скрипт страницы ---
__PAGE__
// --- тесты ---
(async () => { __TESTS__ })().then(() => {
  console.log('__MARK__' + JSON.stringify({ results: __results }) + '__END__');
  const bad = __results.filter(r => !r.ok).length;
  process.exit(bad ? 1 : 0);
}).catch((e) => {
  console.error('исключение тестов: ' + (e && e.stack || e));
  process.exit(2);
});
"""

# Скрипт страницы: последний инлайн-блок `<script>` без src (обычно шаблонный).
_SCRIPT_RE = re.compile(r'<script(?P<open>[^>]*)>(?P<body>.*?)</script>',
                        re.IGNORECASE | re.DOTALL)


def web_unit_run(page: str, tests: str, timeout: float = WEB_UNIT_TIMEOUT) -> dict:
    """
    Прогнать тесты на инлайн-скрипте страницы; вернуть сводку.

    Args:
        page: путь к HTML-странице (берётся последний инлайн-`<script>`) или
            к js-файлу (берётся целиком).
        tests: JS — тело async-функции. Хелперы: `t('имя', cond)` — проверка;
            `el(id)` — узел; `fire(id, 'change')` — вызвать обработчиков;
            `await tick()` — дождаться асинхронных колбэков заглушек;
            `storage(key)` — localStorage; `global.__image_size = url => [w,h]|null`
            — размеры для `Image` (null — картинка битая, стреляет onerror).

    Returns:
        {'passed', 'failed', 'results': [{'name','ok'}], 'log': stderr node}.

    Raises:
        FileNotFoundError: страницы нет, node не установлен.
        RuntimeError: тесты упали до итога или не уложились в `timeout`
            (текст node — в сообщении).
    """
    node = shutil.which('node')
    if not node:
        raise FileNotFoundError('node не найден в PATH — юниты страницы без него не бегут')
    if not os.path.isfile(page):
        raise FileNotFoundError(f'страница не найдена: {page}')

    with open(page, encoding='utf-8', errors='replace') as fh:
        raw = fh.read()
    if page.lower().endswith('.js'):
        script = raw
        file_ids = []
    else:
        blocks = [m for m in _SCRIPT_RE.finditer(raw) if 'src' not in m.group('open').lower()]
        if not blocks:
            raise RuntimeError(f'в {page} нет инлайн-<script> — тестировать нечего')
        script = blocks[-1].group('body')
        file_ids = re.findall(r'<input[^>]*type="file"[^>]*id="([^"]+)"', raw)

    # Порядок замен: подставляемое ищем раньше готового — чтобы `__MARK__`
    # не съелся маркером, случайно всплывшим в тексте страницы или тестов.
    program = (_WEB_UNIT_STUB
               .replace('__FILE_IDS__', json.dumps(file_ids))
               .replace('__PAGE__', script)
               .replace('__TESTS__', tests)
               .replace('__MARK__', _WEB_UNIT_MARK))
    with tempfile.NamedTemporaryFile('w', suffix='.js', encoding='utf-8',
                                     delete=False) as fh:
        fh.write(program)
        path = fh.name
    try:
        try:
            done = subprocess.run([node, path], capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'тесты не завершились за {timeout:g} с (зависший await?)')
    finally:
        os.unlink(path)

    match = re.search(re.escape(_WEB_UNIT_MARK) + r'(.*)__END__', done.stdout)
    if not match:
        tail = (done.stderr or done.stdout).strip()[-400:]
        raise RuntimeError(f'итог не дописан (код {done.returncode}): {tail}')
    results = json.loads(match.group(1))['results']
    return {'passed': sum(1 for r in results if r['ok']),
            'failed': sum(1 for r in results if not r['ok']),
            'results': results, 'log': done.stderr}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Юнит-прогон инлайн-скрипта страницы на node с заглушками DOM.')
    ap.add_argument('page', help='HTML-страница или js-файл')
    ap.add_argument('tests', help='js-файл с тестами (t/fire/tick/storage)')
    ap.add_argument('--json', action='store_true', help='ответить одним JSON')
    ns = ap.parse_args()
    try:
        with open(ns.tests, encoding='utf-8') as fh:
            tests = fh.read()
        res = web_unit_run(ns.page, tests)
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')
    if ns.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        for line in (res['log'] or '').splitlines():
            print('  |', line)
        for r in res['results']:
            print(('ok   ' if r['ok'] else 'FAIL ') + r['name'])
        print(f"пройдено {res['passed']}, провалено {res['failed']}")
    raise SystemExit(1 if res['failed'] else 0)

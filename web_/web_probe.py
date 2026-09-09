"""
JSON-зонд страницы: геометрия и состояние — числами, без снимка и без CDP.

`web_shot` отвечает на «как выглядит», `web_unit` — на «как живёт инлайн-скрипт
в заглушках DOM», а между ними дыра: проверить, куда страница реально поставила
элементы (после её же скриптов, со своими шрифтами и layout'ом), не поднимая
CDP-сессию и не размечая кадр по пикселям. Ответ — `--dump-dom`: Chrome
отдаёт сериализованный DOM после виртуального времени, а зонд врезается в
страницу скриптом, который пишет результат `JSON.stringify` в скрытый
`<pre id="__probe__">`. Функция вынимает узел из дампа и отдаёт объект Python.

Грабли, из-за которых это не однострочник:
* JSON из дампа — всегда после `html.unescape`: сериализация Chrome превращает
  кавычки и амперсанды в сущности, `json.loads` на сыром тексте падает;
* зонд исполняется в конце `<body>`, то есть после всех скриптов страницы —
  посев (`seed_js`) наоборот едет сразу после `<head>`: страница читает
  localStorage на старте, поздно посеянное значение она уже не увидит
  (поздний посев чинят вызовом из зонда ререндера вроде `DIA_RENDER`);
* измеряется момент исполнения зонда (t=0 раскладки), а не момент дампа:
  анимированный transform в значения не попадает — для него budget не help;
* SVG `className` — строка только через `getAttribute('class')`, в зонде про
  svg-селекторы это ловушка для JS-руки.

`web_probe_rects` — самая частая форма зонда: список CSS-селекторов → их
getBoundingClientRect. Координаты viewport-относительные, недотянутые ниже
сгиба элементы видны числами (y за высотой вьюпорта — не ошибка, а факт).
"""
import argparse
import html
import json
import re
import subprocess

from web_.web_shot import (WEB_SHOT_ERR_TAIL, WEB_SHOT_SIZE, WEB_SHOT_TIMEOUT,
                           _find_browser, _served_page)

# Бюджет виртуального времени для дампа, мс: больше снимочных 1200 — зонду
# мало «после анимаций», ему нужны загруженные шрифты (метрики строк!) и
# скрипты страницы до конца.
WEB_PROBE_BUDGET = 2000

# Узел-приёмник зонда в сериализованном DOM.
WEB_PROBE_NODE = '__probe__'

_PROBE_RE = re.compile(r'<pre[^>]*id="%s"[^>]*>(.*?)</pre>' % WEB_PROBE_NODE,
                       re.S)


def web_probe(src, probe_js, seed_js='', size=WEB_SHOT_SIZE,
              budget=WEB_PROBE_BUDGET, chrome='') -> object:
    """
    Исполнить JS на странице и вернуть результат как объект Python.

    Args:
        src: http(s)-URL или путь к локальному HTML (раздаётся со своего
            каталога, как в `web_shot` — корневые ссылки живут).
        probe_js: тело функции-зонда; обязан что-то `return`-нуть (значение и
            вернётся). Исполняется после всех скриптов страницы.
        seed_js: JS сразу после <head>, до скриптов страницы — посев
            localStorage и прочее состояние, которое страница читает на старте.
        size: (ширина, высота) вьюпорта.
        budget: virtual-time-budget мс (шрифты, скрипты; см. модуль).
        chrome: путь к бинарю браузера; пусто — поиск `web_shot`.

    Returns:
        разобранный JSON зонда (обычно dict).

    Raises:
        FileNotFoundError: локальный файл не найден или браузера нет в PATH.
        RuntimeError: Chrome ошибся, зонд не оставил узла (страница не дошла
            до скрипта) или сам зонд упал — текст ошибки внутри.
    """
    browser = chrome or _find_browser()
    is_url = src.startswith(('http://', 'https://'))
    if is_url:
        dom = _dump_dom(browser, src, size, budget)
    else:
        with _served_page(src, _probe_script(probe_js), seed_js) as url:
            dom = _dump_dom(browser, url, size, budget)
    match = _PROBE_RE.search(dom)
    if not match:
        raise RuntimeError('страница не дописала зонд: скрипт не дошёл до '
                           'конца <body> (падает сам или падает страница)')
    payload = json.loads(html.unescape(match.group(1)))
    if isinstance(payload, dict) and '__probe_error__' in payload:
        raise RuntimeError(f'зонд упал: {payload["__probe_error__"]}')
    return payload


def web_probe_rects(src, selectors, **kw) -> dict:
    """Список CSS-селекторов → их getBoundingClientRect (или null).

    Именованные аргументы `web_probe`. Селектор, не найденный страницей, —
    null, а не исключение: отсутствующий элемент — тоже ответ про неё.
    """
    js = ('var out = {}; var S = %s; S.forEach(function (s) {'
          'var e = document.querySelector(s);'
          'if (!e) { out[s] = null; return; }'
          'var r = e.getBoundingClientRect();'
          'out[s] = {x: +r.x.toFixed(1), y: +r.y.toFixed(1),'
          ' width: +r.width.toFixed(1), height: +r.height.toFixed(1)}; });'
          'return out;') % json.dumps(list(selectors), ensure_ascii=False)
    return web_probe(src, js, **kw)


def _probe_script(probe_js) -> str:
    """Обёртка зонда: try/catch, результат — в скрытый <pre> документа."""
    return ("(function () { var out;"
            "try { out = JSON.stringify((function () { %s })()); }"
            "catch (e) { out = JSON.stringify({__probe_error__: String(e)}); }"
            "var pre = document.createElement('pre');"
            "pre.id = '%s'; pre.style.display = 'none';"
            "pre.textContent = out;"
            "document.documentElement.appendChild(pre); })();" % (
                probe_js, WEB_PROBE_NODE))


def _dump_dom(browser, url, size, budget) -> str:
    """Один запуск Chrome --dump-dom; stdout — сериализованный DOM."""
    args = [browser, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--no-first-run', '--no-default-browser-check',
            '--force-device-scale-factor=1',
            f'--window-size={int(size[0])},{int(size[1])}']
    if budget:
        args.append(f'--virtual-time-budget={int(budget)}')
    args += ['--dump-dom', url]
    try:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=WEB_SHOT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'Chrome не завершился за {WEB_SHOT_TIMEOUT:g} с: '
                           f'{url}')
    if done.returncode != 0:
        raise RuntimeError(f'Chrome завершился с кодом {done.returncode}: '
                           f'{done.stderr.strip()[-WEB_SHOT_ERR_TAIL:]}')
    return done.stdout


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='JSON-зонд headless Chrome: JS страницы → JSON '
                    '(body функции; --rect вместо тела для замера селекторов).')
    ap.add_argument('src', help='URL или путь к HTML')
    ap.add_argument('--probe-js', default='',
                    help='тело функции-зонда (с return), исполняется после '
                         'скриптов страницы')
    ap.add_argument('--rect', action='append', default=[],
                    metavar='CSS', help='селектор для getBoundingClientRect, '
                                        'повторять; без --probe-js')
    ap.add_argument('--seed-js', default='',
                    help='JS сразу после <head> (посев localStorage)')
    ap.add_argument('--size', default='1080,2400', help='вьюпорт шириной,высотой')
    ap.add_argument('--budget', type=int, default=WEB_PROBE_BUDGET,
                    help='virtual-time-budget мс')
    ap.add_argument('--chrome', default='', help='путь к бинарю браузера')
    ns = ap.parse_args()
    try:
        w, h = (int(v) for v in ns.size.split(',', 1))
        if not ns.probe_js and not ns.rect:
            raise SystemExit('нужен --probe-js или хотя бы один --rect')
        if ns.rect:
            res = web_probe_rects(ns.src, ns.rect, seed_js=ns.seed_js,
                                  size=(w, h), budget=ns.budget,
                                  chrome=ns.chrome)
        else:
            res = web_probe(ns.src, ns.probe_js, seed_js=ns.seed_js,
                            size=(w, h), budget=ns.budget, chrome=ns.chrome)
        print(json.dumps(res, ensure_ascii=False))
    except (RuntimeError, FileNotFoundError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')

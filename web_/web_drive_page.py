"""Страница как человек: вход из `.env`, встроенный модуль, ожидание с базой.

`web_drive_eval` исполняет JS, но каждый скрипт заново нёс четыре одних и тех
же строки: вход, вынимание встроенного модуля из HTML страницы, вызов его
инициализации и свой `wait`. На каждом бойлерплейте были свои грабли:

* пароль подставляли `sed`-ом в скрипт: пустая переменная в момент подстановки
  — и вход отвечает 422 «вход» вместо ответа страницы; здесь пароль читается
  из `.env` (`config`) в момент вызова, а не в момент сочинения скрипта;
* встроенный импорт в одном шаблоне в двойных кавычках, в другом в одинарных —
  regex с одними кавычками на странице с другими даёт `reading '1' of null`;
  здесь имена и аргумент вынимаются из HTML без угадывания кавычек; а где
  угадывать нечего — там `init=` с модулем и вызовом прямо (автопоиск остался
  запасным: на странице с двумя модулями он норовит иницировать не тот);
* `wait` без базовой линии довольствовался СТАРОЙ таблицей: строка уже стояла
  в DOM от прежнего рендера, условие «строка появилась» было истинно сразу, и
  скрипт уходил читать прежние числа, пока страница ещё перерисовывала новые;
  здесь третий аргумент `wait(fn, ms, base)` ждёт ИЗМЕНЕНИЕ от базы.

Скрипту остаётся само дело: заполнить поле, нажать, вернуть число.
"""
import json

from config.config import config_get
from web_.web_drive import web_drive_eval

# ОБОЛОЧКА страницы: вход (опционально), страница под своим URL, её же модуль и
# его инициализация, `wait` с базовой линией. Всё — телом одной async-функции,
# как любит `web_drive_eval`.
_BOOT = """
const wait = async (fn, ms = 9000, base) => { const t0 = Date.now();
    while (Date.now() - t0 < ms) { const v = fn && fn();
        const hit = base === undefined ? !!v
            : v != null && JSON.stringify(v) !== JSON.stringify(base);
        if (hit) return v; await new Promise(z => setTimeout(z, 120)); }
    return null; };
%(login)s
const pg = await (await fetch(%(url)s, {credentials: 'same-origin'})).text();
%(scan)s
const doc = new DOMParser().parseFromString(pg, 'text/html');
// стили страницы едут в голову шелла, иначе голый body рисуется без них и
// сбивает с толку: модалка без правила [hidden] кажется открытой.
document.head.append(...doc.head.querySelectorAll('style, link[rel=stylesheet]'));
document.body.innerHTML = doc.body.innerHTML;
const mod = await import(src);
await mod[nm](ia);
"""

# Запасной путь, когда `init` не передан: модуль инициализации угадывается по
# разметке. ⚠ шаблоны зовут и `api.js`, и модуль страницы, и `notification.js`;
# верен обычно не первый — поэтому угадывание лишь запас, `init` точнее.
_SCAN = """
const imps = [...pg.matchAll(/import\\s*\\{([^}]+)\\}\\s*from\\s*(["'])(([^"']+)\\2)/g)];
let src = '', nm = '', ia;
for (const m of imps) {
    for (const n of m[1].split(',').map(s => s.trim()).filter(Boolean)) {
        const c = pg.match(new RegExp(n + '\\\\s*\\\\(\\\\s*(["\\'])((.|\\\\n)*?)\\\\1'));
        if (c) { src = m[3]; nm = n; ia = c[2]; break; }
    }
    if (nm) break;
}
if (!nm) return {err: 'на странице нет вызова инициализации — нечего ' +
                     'подключать (зачем подключают — скажи init)'};
"""

_LOGIN = """
const l = await fetch(%(url)s, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(%(body)s), credentials: 'same-origin'});
if (!l.ok) return {err: 'вход', status: l.status};
"""


def web_drive_page(url, script, *, login=None, login_url='/auth/login',
                   wait_ms=20000, chrome='', init=None, shot='',
                   size=()) -> dict:
    """Прогнать скрипт на живой странице без бойлерплейта входа и разметки.

    До тела `script` оболочка сама: входит (учётка берётся из `.env` —
    `ADMIN_USERNAME`/`ADMIN_PASSWORD` — если `login` не передан), открывает
    страницу, вынимает из её HTML встроенный ES-модуль и вызов его
    инициализации (кавычки в шаблоне любые), ставит разметку страницы в DOM и
    подключает модуль. Скрипту доступны `mod` (подключённый модуль страницы),
    `pg` (её HTML) и `wait(fn, ms, base)` — с базовой линией: ждёт и
    возвращает значение, когда `fn()` ОТЛИЧИТСЯ от `base` (или станет
    истинным, без `base`).

    Args:
        url: адрес живой страницы панели (не локальный файл: страница та,
            что под учёткой).
        script: тело async-функции — самое дело; `return <значение>`.
        login: {'username','password','url'} или None — тогда из `.env`;
            False — страница без входа.
        login_url: адрес ручки входа панели.
        wait_ms: потолок ожидания в web_drive_eval.
        chrome: путь к бинарю браузера.
        init: {'file': 'url модуля', 'name': 'функция', 'argument': ...} —
            модуль и вызов инициализации явно; без него модуль угадывается по
            разметке (см. ⚠ в шапке), и на странице с двумя модулями ошибается.
        shot: куда положить PNG страницы после выполнения; пусто — не снимать.

    Returns:
        как `web_drive_eval`: {'value', 'console', 'storage', 'shot'}.
    """
    if login is None:
        login = {'username': config_get('ADMIN_USERNAME', ''),
                 'password': config_get('ADMIN_PASSWORD', '')}
    login_js = ''
    if login:
        body = json.dumps({'username': login.get('username', ''),
                           'password': login.get('password', '')})
        login_js = _LOGIN % {'url': json.dumps(login.get('url', login_url)),
                             'body': body}
    if init:
        arg = ('undefined' if 'argument' not in init
               else json.dumps(init['argument'], ensure_ascii=False))
        scan = (f"const src = {json.dumps(init['file'])}, "
                f"nm = {json.dumps(init['name'], ensure_ascii=False)}, ia = {arg};")
    else:
        scan = _SCAN
    full = _BOOT % {'login': login_js,
                    'url': json.dumps(url, ensure_ascii=False), 'scan': scan}
    # ⚠ Браузер сажаем на СТАРТОВУЮ страницу, а не на целевую: без сессии целевая
    # редиректом уходит на `/login?next=…`, и цель DevTools ищется уже по чужому
    # URL. Под учёткой стартовая — сама ручка входа: оболочка затем сама
    # открывает целевую через `fetch` (см. `_BOOT`).
    from urllib.parse import urlsplit
    p = urlsplit(url)
    start = f'{p.scheme}://{p.netloc}{login_url}' if login else url
    return web_drive_eval(start, full + script, wait_ms=wait_ms, chrome=chrome,
                          shot=shot, size=size or (1080, 2400))


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='Скрипт на живой странице без бойлерплейта: оболочка '
                    'входит (учётка из .env), подключает модуль страницы, '
                    'wait с базовой линией.')
    ap.add_argument('url', help='адрес живой страницы')
    ap.add_argument('-e', '--js', required=True,
                    help='тело async-функции — самое дело (с return)')
    ap.add_argument('--wait-ms', type=int, default=20000)
    ap.add_argument('--init', default='',
                    help='модуль инициализации явно: "файл|имя|аргумент" '
                         '(третья часть — текст, не JSON; пусто — автопоиск)')
    ns = ap.parse_args()
    init = None
    if ns.init:
        mod_file, mod_fn, *mod_arg = ns.init.split('|', 2)
        init = {'file': mod_file, 'name': mod_fn,
                **({'argument': mod_arg[0]} if mod_arg else {})}
    res = web_drive_page(ns.url, ns.js, wait_ms=ns.wait_ms, init=init)
    print(json.dumps(res['value'], ensure_ascii=False))

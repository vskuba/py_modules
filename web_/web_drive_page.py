"""Страница как человек: вход из `.env`, встроенный модуль, ожидание с базой.

`web_drive_eval` исполняет JS, но каждый скрипт заново нёс четыре одних и тех
же строки: вход, вынимание встроенного модуля из HTML страницы, вызов его
инициализации и свой `wait`. На каждом бойлерплейте были свои грабли:

* пароль подставляли `sed`-ом в скрипт: пустая переменная в момент подстановки
  — и вход отвечает 422 «вход» вместо ответа страницы; здесь пароль читается
  из `.env` (`config`) в момент вызова, а не в момент сочинения скрипта;
* встроенный импорт в одном шаблоне в двойных кавычках, в другом в одинарных —
  regex с одними кавычками на странице с другими даёт `reading '1' of null`;
  здесь имена и аргумент вынимаются из HTML без угадывания кавычек;
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
// ⚠ берём НЕ первый импорт: шаблоны зовут и `api.js`, и модуль страницы;
// верен тот, чьё имя здесь же ВЫЗВАНО со строковым аргументом (`xInit("…")`).
const imps = [...pg.matchAll(/import\\s*\\{([^}]+)\\}\\s*from\\s*(["'])(([^"']+)\\2)/g)];
let src = '', nm = '', ia = null;
for (const m of imps) {
    for (const n of m[1].split(',').map(s => s.trim()).filter(Boolean)) {
        const c = pg.match(new RegExp(n + '\\\\s*\\\\(\\\\s*(["\\'])((.|\\\\n)*?)\\\\1'));
        if (c) { src = m[3]; nm = n; ia = c; break; }
    }
    if (nm) break;
}
if (!nm) return {err: 'на странице нет вызова инициализации — нечего подключать'};
document.body.innerHTML =
    new DOMParser().parseFromString(pg, 'text/html').body.innerHTML;
const mod = await import(src);
await mod[nm](ia[2]);
"""

_LOGIN = """
const l = await fetch(%(url)s, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(%(body)s), credentials: 'same-origin'});
if (!l.ok) return {err: 'вход', status: l.status};
"""


def web_drive_page(url, script, *, login=None, login_url='/auth/login',
                   wait_ms=20000, chrome='') -> dict:
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
    full = _BOOT % {'login': login_js, 'url': json.dumps(url,
                                                         ensure_ascii=False)}
    # ⚠ Браузер сажаем на СТАРТОВУЮ страницу, а не на целевую: без сессии целевая
    # редиректом уходит на `/login?next=…`, и цель DevTools ищется уже по чужому
    # URL. Под учёткой стартовая — сама ручка входа: оболочка затем сама
    # открывает целевую через `fetch` (см. `_BOOT`).
    from urllib.parse import urlsplit
    p = urlsplit(url)
    start = f'{p.scheme}://{p.netloc}{login_url}' if login else url
    return web_drive_eval(start, full + script, wait_ms=wait_ms, chrome=chrome)


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
    ns = ap.parse_args()
    res = web_drive_page(ns.url, ns.js, wait_ms=ns.wait_ms)
    print(json.dumps(res['value'], ensure_ascii=False))

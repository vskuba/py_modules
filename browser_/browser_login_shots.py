"""
Войти через форму и снять PNG нескольких страниц — одной командой.

Родился на задаче «приложи к тикету скриншоты дашборда»: вход, переключатель
контекста (бренд, проект, тенант) и три-четыре страницы. Руками это каждый раз
сценарий `browser_` плюс разбор плёнки, а плёнка — не то: JPEG размером с окно,
и одинаковые подряд кадры в неё не попадают. Здесь кадр снимается прямо со
страницы сессии, PNG, по желанию на всю высоту.

## Как устроено

1. Сценарий `browser_scenario_run`: `goto` на форму, `fill` полей значениями из
   `secrets` (`{{имя}}`, в трассу и журнал не попадают), `click` по кнопке.
2. Клики после входа (`clicks`) — тем же сценарием, на той же сессии.
3. Каждая страница: `goto`, пауза, `page.screenshot` в файл.

Креды не передаются аргументами: в аргументах — **имена ключей** (`selector=КЛЮЧ`),
значения читает `config_env` из `.env`/`.env.local` каталога `--root`. Иначе пароль
оседает в истории шелла и в выводе `ps`.

## ⚠ Грабли

* **Контекст из URL ≠ контекст сессии.** SPA часто рисует в шапке то, что
  в адресе (`/wc1/...`), а данные грузит по контексту на сервере, который
  переключается отдельным вызовом *после* отрисовки. Итог — шапка «WC1», таблица
  чужого бренда, и снимок врёт. Переключать контекст кликом в переключателе
  (`clicks`), а не адресом; проверять по содержимому кадра.
* MUI `Select` открывается кликом по `[role=combobox]`, пункты — `li[role=option]`;
  точное совпадение текста — `:text-is("WC1")`.
* Пауза после `goto` (`wait_ms`) — не красота: таблицы SPA догружаются после
  `domcontentloaded`, без паузы снимается пустая сетка.

CLI:

    python -m browser_.browser_login_shots --base https://stage.example.com \\
        --root /path/to/project \\
        --field 'input[name=email]=UI_EMAIL' --field 'input[name=password]=UI_PASSWORD' \\
        --click 'header [role=combobox] >> nth=0' --click 'li[role=option]:text-is("WC1")' \\
        --page '/wc1/settings/cascading=cascading.png' --out /tmp/shots [--full]
"""
import argparse
import asyncio
import os
import sys

from config.config import config_env

from browser_.browser_pool import (
    browser_pool_session_close,
    browser_pool_session_get,
    browser_pool_session_open,
    browser_pool_start,
    browser_pool_stop,
)
from browser_.browser_scenario import BrowserScenario, BrowserScenarioStep
from browser_.browser_scenario_run import browser_scenario_run

BROWSER_LOGIN_SHOTS_WAIT_MS = 6000
BROWSER_LOGIN_SHOTS_VIEWPORT = {'width': 1600, 'height': 900}


async def browser_login_shots(base: str, fields: dict, secrets: dict, pages: list,
                              out_dir: str, login_path: str = '/login',
                              submit: str = 'button[type=submit]', clicks: list | None = None,
                              wait_ms: int = BROWSER_LOGIN_SHOTS_WAIT_MS, full_page: bool = False,
                              viewport: dict | None = None) -> list:
    """Войти формой, сделать клики, снять страницы; вернуть пути PNG.

    Args:
        base: корень сайта без завершающего `/`.
        fields: {селектор поля: имя секрета} — значение подставляется из `secrets`.
        secrets: {имя: значение}; в трассу и журнал сценария не попадают.
        pages: [(путь от base, имя файла PNG)].
        clicks: селекторы, по которым щёлкнуть после входа (переключатель контекста).

    Raises:
        RuntimeError: вход или клик не сработал — с номером шага и ошибкой сценария.
    """
    step = BrowserScenarioStep
    steps = [step(action='goto', url=base + login_path)]
    steps += [step(action='fill', selector=sel, value='{{%s}}' % name) for sel, name in fields.items()]
    steps += [step(action='click', selector=submit), step(action='wait_ms', value=str(wait_ms))]
    for sel in clicks or []:
        steps += [step(action='click', selector=sel), step(action='wait_ms', value='1000')]
    if clicks:
        steps.append(step(action='wait_ms', value=str(wait_ms)))

    await browser_pool_start()
    session = await browser_pool_session_open(name='login-shots',
                                              viewport=viewport or BROWSER_LOGIN_SHOTS_VIEWPORT)
    sid = session['session_id']
    saved = []
    try:
        run = await browser_scenario_run(BrowserScenario(name='login-shots', steps=steps),
                                         session_id=sid, secrets=secrets, film=False)
        if run['status'] != 'ok':
            raise RuntimeError(f"шаг {run.get('step_no')}: {run.get('error')} (адрес {run.get('url')})")
        page = browser_pool_session_get(sid)['page']
        os.makedirs(out_dir, exist_ok=True)
        for path, name in pages:
            await page.goto(base + path, wait_until='domcontentloaded')
            await page.wait_for_timeout(wait_ms)
            target = os.path.join(out_dir, name)
            await page.screenshot(path=target, full_page=full_page)
            saved.append(target)
    finally:
        await browser_pool_session_close(sid)
        await browser_pool_stop()
    return saved


def _cli(argv=None):
    ap = argparse.ArgumentParser(description='Войти через форму и снять PNG страниц.')
    ap.add_argument('--base', required=True)
    ap.add_argument('--login', default='/login', help='путь формы входа')
    ap.add_argument('--root', default='.', help='каталог проекта с .env/.env.local для ключей --field')
    ap.add_argument('--field', action='append', required=True, metavar='СЕЛЕКТОР=КЛЮЧ',
                    help='поле формы и имя ключа env-файла со значением')
    ap.add_argument('--submit', default='button[type=submit]')
    ap.add_argument('--click', action='append', default=[], help='клик после входа (по порядку)')
    ap.add_argument('--page', action='append', required=True, metavar='ПУТЬ=ФАЙЛ.png')
    ap.add_argument('--out', required=True, help='каталог для PNG')
    ap.add_argument('--wait-ms', type=int, default=BROWSER_LOGIN_SHOTS_WAIT_MS)
    ap.add_argument('--full', action='store_true', help='страница целиком, с прокруткой')
    a = ap.parse_args(argv)

    env = {**config_env(a.root), **os.environ}
    fields, secrets = {}, {}
    for item in a.field:
        sel, _, key = item.rpartition('=')
        if not env.get(key):
            sys.exit(f'ключ {key} пуст или не найден в {a.root}/.env*')
        name = key.lower()
        fields[sel], secrets[name] = name, env[key]
    pages = [tuple(p.rsplit('=', 1)) for p in a.page]
    try:
        for path in asyncio.run(browser_login_shots(
                a.base.rstrip('/'), fields, secrets, pages, a.out, a.login, a.submit,
                a.click, a.wait_ms, a.full)):
            print(path)
    except RuntimeError as err:
        sys.exit(f'login-shots: {err}')


if __name__ == '__main__':
    _cli()

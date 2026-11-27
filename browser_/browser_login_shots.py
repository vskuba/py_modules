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
3. Каждая страница: `goto`, пауза, клики по странице (`page_clicks` — открыть
   карточку, раскрыть строку), `page.screenshot` в файл.
4. Метки (`marks`): селектор → `bounding_box` в браузере → стрелка с подписью
   поверх PNG (`image_arrows`). Координаты из браузера, не на глаз; у снимка на
   всю высоту к ним прибавляется прокрутка.
5. Кадры для документов: `crop` режет снимок по элементу (карточка без шапки
   и меню), `blur` размывает селекторы с PII (`image_fix_blur`) — раскладка
   кадра остаётся читаемой, значение — нет.
6. PII-скан: `document.body.innerText` каждой страницы прогоняется через
   `browser_login_shots_pii`; найденное (email/IP/PAN, значения не длиннее
   10 символов) ложится в `pii-report.md` рядом со снимками — проверка, что
   `blur` закрыл всё, читается одной строкой, а не девятью кадрами.

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
* Селектор из `blur` не нашёл элемент — снимок **не делается**: кадр с
  непромазанным PII наружу не отдаётся, падение честнее молчаливой утечки.
  То же, если PII-окно легло вне кадра (например, кроп отрезал шапку с
  адресом): кадр правится во временном файле и переставляется на место лишь
  после успешного blur.
* PII-скан — отчёт, а не замок: он видит значение в тексте страницы, но не
  знает, попало ли оно в кадр и закрыто ли `blur`. Отчёт читают вместе с
  кадрами.
* Метки, кроп и блюр на снимке окна делят одну прокрутку: кадр останется там,
  куда доехал прокручиваемый блок во время сбора боксов, — порядок аргументов
  важен; для длинных страниц надёжнее `--full`.

CLI:

    python -m browser_.browser_login_shots --base https://stage.example.com \\
        --root /path/to/project \\
        --field 'input[name=email]=UI_EMAIL' --field 'input[name=password]=UI_PASSWORD' \\
        --click 'header [role=combobox] >> nth=0' --click 'li[role=option]:text-is("WC1")' \\
        --page '/wc1/settings/cascading=cascading.png' --out /tmp/shots [--full] \\
        --page-click 'payouts.png|tr:has-text("a54422b0")' \\
        --mark 'cascading.png|tr:has-text("Nuvei")|Nuvei payout in cascading' \\
        --blur 'header .user-email' --blur '03-payouts.png|td:nth-child(3)' \\
        --crop '08-card.png|div.MuiDialog-container' --pii 'cid=\\bcid\\b\\s*\\d+'
"""
import argparse
import asyncio
import os
import re
import sys

from config.config import config_env

from browser_.browser_pool import (
    browser_pool_session_close,
    browser_pool_session_get,
    browser_pool_session_open,
    browser_pool_start,
    browser_pool_stop,
)
from browser_.scenario.browser_scenario import BrowserScenario, BrowserScenarioStep
from browser_.scenario.browser_scenario_run import browser_scenario_run
from image_.image_arrow import image_arrows
from image_.image_fix import image_fix_blur

BROWSER_LOGIN_SHOTS_WAIT_MS = 6000
BROWSER_LOGIN_SHOTS_VIEWPORT = {'width': 1600, 'height': 900}
BROWSER_LOGIN_SHOTS_BLUR_RADIUS = 9   # та же σ, что IMAGE_FIX_BLUR_RADIUS
# Что ищем в тексте страницы. Значение в отчёт уходит префиксом ≤10 символов
# (правило data_mask: наружу — только отпечаток). IPv4 ловит и номера версий —
# отчёт для разбора человеком, не приговор.
BROWSER_LOGIN_SHOTS_PII = {
    'email': r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
    'ipv4': r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
    'pan': (r'(?<!\d)(?:4\d{12,18}|5[1-5]\d{14}|6(?:011|5\d{1,3})\d{8,11}|3[47]\d{13})'
            r'(?!\d)'),
    'masked_pan': r'\b\d{4,8}\*{4,12}\d{1,4}\b',
}
BROWSER_LOGIN_SHOTS_PII_PREFIX = 10


async def browser_login_shots(base: str, fields: dict, secrets: dict, pages: list,
                              out_dir: str, login_path: str = '/login',
                              submit: str = 'button[type=submit]', clicks: list | None = None,
                              wait_ms: int = BROWSER_LOGIN_SHOTS_WAIT_MS, full_page: bool = False,
                              viewport: dict | None = None, page_clicks: dict | None = None,
                              marks: dict | None = None, blur: dict | None = None,
                              crop: dict | None = None, pii: dict | None = None,
                              blur_radius: int = BROWSER_LOGIN_SHOTS_BLUR_RADIUS) -> list:
    """Войти формой, сделать клики, снять страницы; вернуть пути PNG.

    Args:
        base: корень сайта без завершающего `/`.
        fields: {селектор поля: имя секрета} — значение подставляется из `secrets`.
        secrets: {имя: значение}; в трассу и журнал сценария не попадают.
        pages: [(путь от base, имя файла PNG)].
        clicks: селекторы, по которым щёлкнуть после входа (переключатель контекста).
        page_clicks: {имя файла: [селектор]} — клики на странице перед снимком.
        marks: {имя файла: [(селектор, подпись)]} — стрелки на элементы после снимка.
        blur: {имя файла | '*': [селектор]} — размыть эти области на снимке (PII).
        crop: {имя файла | '*': селектор} — обрезать снимок по элементу.
        pii: {имя файла | '*': {вид: регулярка}} — дополнить стандартный поиск
            PII; страницы сканируются всегда, hits — в `pii-report.md` в `out_dir`.
        blur_radius: sigma гауссианы blur, px.

    Raises:
        RuntimeError: вход или клик не сработал — с номером шага и ошибкой сценария;
            метка или PII-селектор не нашли элемент либо blur-окно легло вне
            кадра — с именем файла и селектором, кадр под именем не остаётся.
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
    hits_by_file: dict = {}  # уже до try: отчёт в finally обязан видеть и оборванный прогон
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
            for sel in (page_clicks or {}).get(name, []):
                await page.locator(sel).first.click()
                await page.wait_for_timeout(wait_ms // 2)
            crop_sel = (crop or {}).get(name) or (crop or {}).get('*')
            blur_sels = [s for k in (name, '*') for s in (blur or {}).get(k, [])]
            # all=True для blur: селектор колонки (`td:nth-child(3)`) находит
            # каждую ячейку — размывать только первую значит сдать PII остальных
            specs = ([(crop_sel, '', False)] if crop_sel else []) \
                + [(s, '', True) for s in blur_sels] \
                + [(s, l, False) for s, l in (marks or {}).get(name, [])]
            # однократный сбор боксов: метки, кроп и блюр видят одну и ту же
            # прокрутку — два вызова _boxes на снимке окна разъехались бы
            groups = await _boxes(page, specs, name, full_page)
            n_crop = 1 if crop_sel else 0
            crop_box = groups[0][0]['box'] if n_crop else None
            blur_boxes = [b for g in groups[n_crop:n_crop + len(blur_sels)] for b in g]
            mark_boxes = [b for g in groups[n_crop + len(blur_sels):] for b in g]
            text = await page.evaluate('document.body ? document.body.innerText : ""')
            hits_by_file[name] = browser_login_shots_pii(
                text, {**BROWSER_LOGIN_SHOTS_PII, **(pii or {}).get('*', {}),
                       **(pii or {}).get(name, {})})
            # ⚠ Снимок пишется во временный файл и переставляется на место
            # после кропа и blur: сбой правки не должен оставлять на диске
            # кадр с непромазанным PII под итоговым именем.
            target = os.path.join(out_dir, name)
            # ⚠ временный кадр правится PIL'ом — расширение обязано быть .png:
            # save() по неизвестному суффиксу не угадывает формат и падает
            raw = os.path.join(out_dir, '.' + name + '.tmp.png')
            await page.screenshot(path=raw, full_page=full_page)
            try:
                ox = oy = 0
                if crop_box:
                    ox, oy = int(crop_box[0]), int(crop_box[1])
                    _crop_png(raw, (ox, oy, ox + int(crop_box[2]), oy + int(crop_box[3])))
                if blur_boxes:
                    got = image_fix_blur(raw, raw,
                                         [(m['box'][0] - ox, m['box'][1] - oy,
                                           m['box'][0] + m['box'][2] - ox,
                                           m['box'][1] + m['box'][3] - oy) for m in blur_boxes],
                                         radius=blur_radius)
                    if got['boxes'] < len(blur_boxes):
                        raise RuntimeError(
                            f'{name}: размываемых окон {got["boxes"]} из {len(blur_boxes)} — '
                            f'PII-окно легло вне кадра (кроп?), кадр не пишется')
                if mark_boxes:
                    image_arrows(raw, [{'box': (m['box'][0] - ox, m['box'][1] - oy,
                                                m['box'][2], m['box'][3]),
                                        'label': m['label']} for m in mark_boxes])
                os.replace(raw, target)
            except BaseException:
                if os.path.exists(raw):
                    os.remove(raw)
                raise
            saved.append(target)
    finally:
        # отчёт обязан догнать и оборванный прогон: кадры первых страниц уже
        # на диске, без строки «что засвечено» их не проверить глазами
        if hits_by_file:
            browser_login_shots_pii_report(out_dir, hits_by_file)
        await browser_pool_session_close(sid)
        await browser_pool_stop()
    return saved


def browser_login_shots_pii(text: str, patterns: dict | None = None) -> list:
    """PII в тексте страницы: [{'kind','value'}] — значение усечено до 10 символов.

    Снимок бэк-офиса — доказательство, а доказательство с открытыми email'ами
    и кардами клиентов наружу не отдаётся. Прежде чем греть PIL'ом, надо
    узнать, что именно засвечено: скан текста страницы отвечает списком за
    секунду, без Playwright и без кадров.

    Args:
        text: `document.body.innerText` (или любой другой текст).
        patterns: {вид: регулярка}; пусто — `BROWSER_LOGIN_SHOTS_PII`.

    Returns:
        [{'kind', 'value'}], где value — префикс ≤10 символов: полный адрес
        или PAN в отчёт не попадают никогда.

    ⚠ Регулярки смотрят на текст страницы, а не на кадр: значение может быть
      в DOM и не попасть в окно снимка (и наоборот — запечься картинкой,
      которой в тексте нет). Отчёт — повод посмотреть кадр, а не замена
      взгляду.
    """
    out = []
    for kind, pat in (patterns or BROWSER_LOGIN_SHOTS_PII).items():
        for m in re.finditer(pat, text):
            out.append({'kind': kind, 'value': m.group(0)[:BROWSER_LOGIN_SHOTS_PII_PREFIX]})
    return out


def browser_login_shots_pii_report(out_dir: str, hits_by_file: dict) -> str:
    """Списать найденное в `out_dir/pii-report.md`; вернуть путь ('' — чисто).

    Формат — строка `файл.png: вид «значение»` на hit: файл читается глазами
    рядом с отчётом, а по «вид» видно, какой селектор надо добавить в --blur.
    """
    if not any(hits_by_file.values()):
        return ''
    report = os.path.join(out_dir, 'pii-report.md')
    with open(report, 'w', encoding='utf-8') as fh:
        fh.write('# PII в снимках\n\n')
        for name, hits in hits_by_file.items():
            for hit in hits:
                fh.write(f'{name}: {hit["kind"]} «{hit["value"]}»\n')
    return report


async def _boxes(page, specs: list, name: str, full_page: bool) -> list:
    """Спеки `(селектор, подпись, all)` → список списков боксов, по одному на спеку.

    `all=False` — первый матч (стрелка метки); `all=True` — каждый найденный
    элемент (PII blur: одна размываемая строка таблицы не закрывает колонку).
    Ноль найденных — RuntimeError; для `all=True` невидимый матч (бокса нет)
    пропускается: в кадре его пикселей всё равно нет.
    """
    if specs:
        # ⚠ Таблицы SPA прокручиваются внутри своего контейнера, а не окна: снимок
        # «на всю высоту» их не раскрывает, и строка ниже первого экрана в кадр не
        # попадает. Прокрутка к первому элементу двигает и такой контейнер.
        # По центру, а не «лишь бы видно»: у края кадра стрелке и подписи некуда встать.
        await page.locator(specs[0][0]).first.evaluate("e => e.scrollIntoView({block: 'center'})")
        await page.wait_for_timeout(500)
    scroll = await page.evaluate('[window.scrollX, window.scrollY]') if full_page else [0, 0]
    height = (await page.evaluate('document.documentElement.scrollHeight') if full_page
              else page.viewport_size['height'])
    groups = []
    for sel, label, every in specs:
        loc = page.locator(sel)
        count = await loc.count()
        if not count:
            raise RuntimeError(f'{name}: селектор «{sel}» ничего не нашёл')
        group = []
        for j in range(count if every else 1):
            box = await loc.nth(j).bounding_box()
            if not box:
                if every:
                    continue  # в DOM, но не отрисован — пикселей в кадре нет
                raise RuntimeError(f'{name}: селектор «{sel}» не нашёл видимый элемент')
            # ⚠ Элемент в DOM, но прокручен за край окна (модалка с прокруткой):
            # стрелка указала бы в пустоту, а PII остался бы не замазан.
            y = box['y'] + scroll[1]
            if y + box['height'] < 0 or y > height:
                raise RuntimeError(f'{name}: «{sel}» вне кадра (y={int(y)}, кадр {height}) — '
                                   f'элемент в другом прокручиваемом блоке или ниже сгиба; '
                                   f'для длинных страниц — --full')
            group.append({'box': (box['x'] + scroll[0], box['y'] + scroll[1],
                                  box['width'], box['height']), 'label': label})
        if not group:
            raise RuntimeError(f'{name}: селектор «{sel}» — только невидимые элементы, '
                               f'размывать нечего, кадр не пишется')
        groups.append(group)
    return groups


def _crop_png(path: str, box: tuple) -> None:
    """Обрезать PNG по окну (x0, y0, x1, y1) на месте."""
    from PIL import Image

    with Image.open(path) as im:
        im.crop(tuple(int(v) for v in box)).save(path)


def _by_file(items: list) -> dict:
    """'ФАЙЛ.png|ЗНАЧЕНИЕ' | 'ЗНАЧЕНИЕ' → {ФАЙЛ | '*': [ЗНАЧЕНИЕ]}; порядок живёт."""
    out = {}
    for item in items:
        name, sep, value = item.partition('|')
        out.setdefault(name if sep else '*', []).append(value if sep else name)
    return out


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
    ap.add_argument('--width', type=int, default=BROWSER_LOGIN_SHOTS_VIEWPORT['width'])
    ap.add_argument('--height', type=int, default=BROWSER_LOGIN_SHOTS_VIEWPORT['height'],
                    help='высота окна: таблица во внутреннем контейнере выше окна не раскроется, '
                         'и последней строке негде оставить место под стрелку')
    ap.add_argument('--page-click', action='append', default=[], metavar='ФАЙЛ.png|СЕЛЕКТОР',
                    help='клик на странице перед её снимком (по порядку)')
    ap.add_argument('--mark', action='append', default=[], metavar='ФАЙЛ.png|СЕЛЕКТОР|ПОДПИСЬ',
                    help='стрелка с подписью на элемент страницы')
    ap.add_argument('--blur', action='append', default=[], metavar='ФАЙЛ.png|СЕЛЕКТОР',
                    help='размыть область селектора на снимке (PII); без имени файла — на всех')
    ap.add_argument('--blur-radius', type=int, default=BROWSER_LOGIN_SHOTS_BLUR_RADIUS,
                    help='blur: sigma гауссианы, px')
    ap.add_argument('--crop', action='append', default=[], metavar='ФАЙЛ.png|СЕЛЕКТОР',
                    help='обрезать снимок по элементу (карточка без шапки и меню)')
    ap.add_argument('--pii', action='append', default=[], metavar='ИМЯ=РЕГУЛЯРКА',
                    help='добавить вид PII к скану текста страницы (в отчёт); '
                         'через | — только для одного файла')
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
    page_clicks, marks = {}, {}
    for item in a.page_click:
        name, _, sel = item.partition('|')
        page_clicks.setdefault(name, []).append(sel)
    for item in a.mark:
        name, sel, label = (item.split('|', 2) + ['', ''])[:3]
        marks.setdefault(name, []).append((sel, label))
    blur, crop = _by_file(a.blur), {k: v[-1] for k, v in _by_file(a.crop).items()}
    pii = {}
    for item in a.pii:
        kind, sep, pat = item.partition('=')        # паттерн — всё после первого '='
        if not sep or not pat:
            sys.exit(f'pii {item!r}: ждётся ИМЯ=РЕГУЛЯРКА или ФАЙЛ.png|ИМЯ=РЕГУЛЯРКА')
        file_, _, kind = kind.rpartition('|')
        if not kind:
            sys.exit(f'pii {item!r}: пустое имя вида PII')
        file_ = file_ or '*'
        try:
            re.compile(pat)
        except re.error as err:
            sys.exit(f'pii {item!r}: {err}')
        pii.setdefault(file_, {})[kind] = pat
    try:
        for path in asyncio.run(browser_login_shots(
                a.base.rstrip('/'), fields, secrets, pages, a.out, a.login, a.submit,
                a.click, a.wait_ms, a.full, {'width': a.width, 'height': a.height},
                page_clicks, marks, blur, crop, pii, a.blur_radius)):
            print(path)
    except RuntimeError as err:
        sys.exit(f'login-shots: {err}')


if __name__ == '__main__':
    _cli()

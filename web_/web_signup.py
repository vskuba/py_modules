"""Регистрация на чужом сайте: разобрать форму, заполнить личностью, вынести вердикт.

Вопрос, на который отвечает модуль: «пускает ли площадка автоматическое
заведение аккаунта и чем именно ответила». Форма чужая, поля неизвестны, а
ответ надо разобрать: готово / письмо-подтверждение / стена / дубликат /
отказ — и не принять одно за другое.

## Границы, они же правила

* **Стена — стоп.** Капча, JS-челлендж, телефон, верификация личности — это
  вердикт `wall` с классом из `web_wall_scan`; долбить её автоматизмом модуль
  не пробует и звать солверы не просит.
* **Один аккаунт на площадку.** С ключом `ledger` модуль сначала читает реестр:
  если у домена уже стоит строка `kind='account'` — отказ, не глядя на форму.
  Множить анонимные аккаунты с одного отпечатка — путь к бану всей площадки.
* **Вердикт `done` — не проверка.** Проверка — публичная страница профиля
  (`web_peek_markers` без куки) и кадр (`web_shot`); вердикт лишь говорит, что
  ответ похож на успех.

## Что здесь есть

* `web_signup_form` — разобрать форму: поля, обязательные, honeypot, csrf, стена;
* `web_signup_try` — полный круг: GET формы (csrf-токен из неё же, тем же
  клиентом), заполнение, POST, вердикт.

Личность — словарь `{'email','login','name','first','last','phone','password'}`;
прочие поля формы закрываются прямым попаданием имени в ключ словаря.
"""
import json
import re
import sys
from urllib.parse import urljoin, urlsplit

from http_.http_pool import http_pool_client
from web_.web_ledger import web_ledger_get
from web_.web_wall import WEB_WALL_UA, web_wall_scan

# слова повторной регистрации: «уже такой есть» — не отказ и не успех
WEB_SIGNUP_DUP = re.compile(
    r'уже (зарегистрир|создан|существу)|вже (зареєстру|створено|існує)'
    r'|already (registered|exists|taken)|is (already )?(taken|registered)'
    r'|email already|дубликат', re.I)
# слова «посмотрите почту»: регистрация ушла в подтверждение адресом
WEB_SIGNUP_MAIL = re.compile(
    r'подтверд\w*.{0,40}(почт|email|адрес)|підтверд\w*.{0,40}(пошт|електронн)'
    r'|check your (e-?mail|inbox)|verify your e-?mail|confirm your (e-?mail|account)'
    r'|activat\w+ (link|page)|письмо отправлен|листа надіслано', re.I)
WEB_SIGNUP_CSRF = re.compile(r'csrf|token|authenticity|nonce', re.I)
# обязательный чекбокс согласия — часть регистрации; его ставим сами;
# несогласие же на маркетинг (без required) не трогаем: личностью не закрыто
WEB_SIGNUP_CONSENT = re.compile(r'agree|term|consent|policy|accept', re.I)
# honeypot: скрытое «человеческое» поле — ловушка на бота; заполнять нельзя
WEB_SIGNUP_HP = re.compile(r'website|homepage|company|fax|hp_|web_?url', re.I)
# порядок значим: подтверждение и пароль раньше логина, имя — самым общим
WEB_SIGNUP_KEYS = (
    ('confirm', ('passwordconfirmation', 'confirmpassword', 'passwordconfirm',
                 'password2', 'rpassword', 'verifypassword')),
    ('password', ('password', 'passwd', 'pass', 'newpassword', 'password1')),
    ('phone', ('phonenumber', 'telephone', 'phone', 'mobile', 'tel')),
    ('email', ('emailaddress', 'useremail', 'email', 'mail')),
    ('login', ('username', 'login', 'nickname', 'nick', 'handle', 'accountname', 'user')),
    ('first', ('firstname', 'givenname', 'first')),
    ('last', ('lastname', 'surname', 'familyname', 'last')),
    ('name', ('fullname', 'realname', 'displayname', 'name')),
)


async def web_signup_form(url: str, proxy: str = '', timeout: float = 15) -> dict:
    """Разобрать форму регистрации: поля, csrf, honeypot, обязательные, стена.

    Args:
        url: адрес страницы с формой регистрации.
        proxy: адрес выхода (`http_proxy_url`) — тем же ходом GET и всё далее.
        timeout: секунд на запрос.

    Returns:
        {'ok': bool, 'action': адрес отправки, 'method': 'GET'|'POST',
         'fields': {имя: {'type','value','required','kind','skip'}},
         'wall': словарь из `web_wall_scan`, 'status': код, 'url': финальный}
        kind — на что поле похоже из перечня `WEB_SIGNUP_KEYS`; skip — причина
        не заполнять ('honeypot', 'submit', 'csrf', ...).
    """
    async with http_pool_client(timeout=timeout, proxy=proxy, follow_redirects=True,
                                headers={'User-Agent': WEB_WALL_UA}) as c:
        r = await c.get(url)
    body = {'status': r.status_code, 'url': str(r.url),
            'wall': web_wall_scan(r.text, status=r.status_code, headers=r.headers)}
    form = _signup_pick(r.text)
    if not form:
        return {'ok': False, 'action': '', 'method': '', 'fields': {}, **body}
    return {'ok': True,
            'action': urljoin(str(r.url), _attr(form, 'action') or str(r.url)),
            'method': (_attr(form, 'method') or 'POST').upper(),
            'fields': _signup_fields(form), **body}


async def web_signup_try(url: str, person: dict, password: str = '',
                         proxy: str = '', ledger: str = '', timeout: float = 20,
                         force: bool = False) -> dict:
    """Один круг регистрации: форма → заполнение → ответ → вердикт.

    Args:
        url: адрес страницы регистрации (форма найдётся сама).
        person: {'email','login','name','first','last','phone'}; прочее —
            прямым именем поля формы.
        password: пароль; без него `person['password']`; нигде — поле останется
            незакрытым и вердикт скажет `fields`.
        proxy: адрес выхода — им же GET формы и POST: csrf-пре-сессия привязана
            к IP, сменить выход между GET и POST — гарантированный отказ.
        ledger: файл реестра (`web_ledger`); до попытки читается лицо домена.
        force: True — идти, даже если домен уже в реестре (не нужно без причины).

    Returns:
        {'ok': bool, 'verdict': 'done'|'mail'|'wall'|'duplicate'|'denied'|
         'limit'|'fields'|'busy', 'site', 'status', 'url', 'sent': [имя...],
         'missing': [...], 'wall': {...}, 'error': словами}
        `done`/`mail` — сигнал записать аккаунт в реестр (web_ledger_put,
        kind='account') и **проверить публичной страницей**, а не поверить ответу.
    """
    site = urlsplit(url).hostname or url
    if ledger and not force:
        got = web_ledger_get(ledger, key=site)
        busy = [x for x in got['rows'] if x.get('kind') == 'account']
        if busy:
            return {'ok': False, 'verdict': 'busy', 'site': site, 'status': 0,
                    'url': url, 'sent': [], 'missing': [], 'wall': {},
                    'error': f'площадка уже освоена (login={busy[0].get("login", "?")}); '
                             'второй аккаунт с того же отпечатка — путь к бану, '
                             'force=True только по осознанному решению'}
    async with http_pool_client(timeout=timeout, proxy=proxy, follow_redirects=True,
                                headers={'User-Agent': WEB_WALL_UA}) as c:
        g = await c.get(url)
        form = _signup_pick(g.text)
        if not form:
            return {'ok': False, 'verdict': 'denied', 'site': site,
                    'status': g.status_code, 'url': str(g.url), 'sent': [],
                    'missing': [], 'wall': web_wall_scan(g.text, g.status_code, g.headers),
                    'error': 'форма регистрации на странице не найдена'}
        fields = _signup_fields(form)
        payload, sent, missing = _signup_payload(fields, person, password)
        if missing:
            return {'ok': False, 'verdict': 'fields', 'site': site,
                    'status': g.status_code, 'url': str(g.url), 'sent': sent,
                    'missing': missing, 'wall': {},
                    'error': 'обязательные поля не закрыты личностью'}
        action = urljoin(str(g.url), _attr(form, 'action') or str(g.url))
        method = (_attr(form, 'method') or 'POST').upper()
        r = (await c.post(action, data=payload) if method == 'POST'
             else await c.get(action, params=payload))
    wall = web_wall_scan(r.text, status=r.status_code, headers=r.headers)
    out = {'site': site, 'status': r.status_code, 'url': str(r.url),
           'sent': sent, 'missing': [], 'wall': wall}
    if wall['wall'] == 'limit':
        return {**out, 'ok': False, 'verdict': 'limit',
                'error': f'окно ждём до {wall["wait"]} с'}
    if wall['wall']:
        return {**out, 'ok': False, 'verdict': 'wall',
                'error': 'площадка требует ручного прохода: ' + '; '.join(wall['evidence'])}
    if WEB_SIGNUP_DUP.search(r.text):
        return {**out, 'ok': False, 'verdict': 'duplicate', 'wall': {},
                'error': 'площадка говорит, что такой аккаунт уже есть'}
    if WEB_SIGNUP_MAIL.search(r.text):
        return {**out, 'ok': True, 'verdict': 'mail', 'wall': {},
                'error': 'подтверждение ушло на почту — дальше mail_wait'}
    alert = re.search(r'alert[^>]*>(?:<i[^>]*></i>)?\s*([^<]{5,140})<', r.text, re.S | re.I)
    if r.status_code >= 400 or alert:
        return {**out, 'ok': False, 'verdict': 'denied', 'wall': {},
                'error': (alert.group(1).strip() if alert else f'status {r.status_code}')}
    return {**out, 'ok': True, 'verdict': 'done', 'wall': {}, 'error': ''}


def _signup_pick(html: str) -> str:
    """Блок формы на странице: с паролем — регистрация, без — не она."""
    best, score = '', 0
    for chunk in re.split(r'<form', html, flags=re.I)[1:]:
        block = '<form' + chunk.split('</form>', 1)[0]
        s = 2 if re.search(r'type="password"', block, re.I) else 0
        s += 1 if re.search(r'type="email"|name="[^"]*email', block, re.I) else 0
        s += 1 if re.search(r'type="submit"|<button', block, re.I) else 0
        if s > score:
            best, score = block, s
    return best if score else ''


def _signup_fields(form: str) -> dict:
    """Поля формы кучей: имя → тип, значение, обязательность, подозреваемый вид."""
    fields: dict = {}
    for tag in re.findall(r'<(?:input|textarea)[^>]*>', form, re.I):
        name = _attr(tag, 'name')
        if not name:
            continue
        typ = (_attr(tag, 'type') or 'text').lower()
        fields[name] = {'type': typ, 'value': _attr(tag, 'value') or '',
                        'required': _attr(tag, 'required') is not None,
                        'kind': _signup_kind(name), 'skip': ''}
        if typ in ('submit', 'image', 'reset', 'button'):
            fields[name]['skip'] = 'submit'
        elif typ == 'hidden':
            fields[name]['skip'] = 'csrf' if WEB_SIGNUP_CSRF.search(name) else (
                'honeypot' if not fields[name]['value'] and WEB_SIGNUP_HP.search(name)
                else 'hidden')
    for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form,
                         re.I | re.S):
        opts = re.findall(r'<option[^>]*value="([^"]*)"', m.group(2), re.I)
        fields[m.group(1)] = {'type': 'select', 'value': opts[0] if opts else '',
                              'required': 'required' in m.group(0).lower(),
                              'kind': _signup_kind(m.group(1)), 'skip': ''}
    for f in fields.values():
        if f['type'] in ('checkbox', 'radio') and not f['value']:
            f['value'] = 'on'
    return fields


def _signup_payload(fields: dict, person: dict, password: str) -> tuple:
    """Что отправляем: собранное тело, имена посланных, список незакрытых."""
    who = {k.lower(): str(v) for k, v in person.items()}
    if not who.get('name') and (who.get('first') or who.get('last')):
        who['name'] = f"{who.get('first', '')} {who.get('last', '')}".strip()
    if password:
        who['password'] = password
    if who.get('password'):
        who.setdefault('confirm', who['password'])
    body, sent, missing = {}, [], []
    for name, f in fields.items():
        if f['skip']:
            if f['skip'] in ('csrf', 'hidden') and f['value']:
                body[name] = f['value']
            continue
        val = who.get(re.sub(r'[^a-z0-9]', '', name.lower()), '') or \
            (who.get(f['kind'], '') if f['kind'] else '')
        if not val and f['type'] in ('checkbox', 'radio') and f['required'] and \
                WEB_SIGNUP_CONSENT.search(name):
            val = 'on'  # обязательное согласие — часть регистрации, ставим сами
        if not val and f['type'] == 'select' and f['value']:
            val = f['value']
        if val:
            body[name] = str(val)
            sent.append(name)
        elif f['required']:
            missing.append(name)
    return body, sent, missing


def _signup_kind(name: str) -> str:
    """На что похоже поле: по нормализованному имени, по порядку WEB_SIGNUP_KEYS."""
    key = re.sub(r'[^a-z0-9]', '', name.lower())
    for kind, names in WEB_SIGNUP_KEYS:
        if key in names:
            return kind
    for kind, names in WEB_SIGNUP_KEYS:
        if any(n in key or key in n for n in names):
            return kind
    return ''


def _attr(tag: str, attr: str) -> str | None:
    """Атрибут тега формы: двойные, одиночные кавычки или голый флаг."""
    m = re.search(rf'{attr}\s*=\s*"([^"]*)"', tag, re.I) or \
        re.search(rf"{attr}\s*=\s*'([^']*)'", tag, re.I)
    if m:
        return m.group(1)
    return '' if re.search(rf'\b{attr}\b', tag, re.I) else None


if __name__ == '__main__':
    import asyncio
    a = sys.argv[1:]
    if len(a) < 2:
        print('употребление: python -m web_.web_signup form URL [--proxy адрес]')
        print('               python -m web_.web_signup try URL --person \'{"email":"..."}\''
              ' [--password ПАР] [--proxy адрес] [--ledger файл]')
        raise SystemExit(2)
    kv = {a[i]: a[i + 1] for i in range(len(a) - 1) if a[i].startswith('--')}
    if a[0] == 'form':
        print(asyncio.run(web_signup_form(a[1], proxy=kv.get('--proxy', ''))))
    else:
        print(asyncio.run(web_signup_try(
            a[1], json.loads(kv.get('--person', '{}')), password=kv.get('--password', ''),
            proxy=kv.get('--proxy', ''), ledger=kv.get('--ledger', ''))))

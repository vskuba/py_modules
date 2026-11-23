"""Стена или ответ: капча, JS-челлендж, телефон, верификация, лимит, блок.

Стена — не ошибка сети и не промах пароля: дальше этого места автоматизм идти
не должен. Модуль отвечает на вопрос «во что упёрся ответ» и предписывает
действие: `human` (стоп, нужен ручной проход), `wait` (окно rate-limit само
пройдёт), `change_exit` (замок антибрутфорса — пара НЕ проверена), `none`
(проходим дальше).

Солверов капчи здесь нет **намеренно**: обходить проверку «ты не робот» — значит
обходить правила площадки. Задача модуля — увидеть стену ДО того, как начнут
долбить, и остановить круг с объяснением в `evidence`.

## Что здесь есть

* `web_wall_scan` — вердикт по html/статусу/заголовкам, без сети;
* `web_wall_probe` — то же на живой адрес: GET своим выходом и вердикт.

Порядок разбора важен: лимит узнаётся до содержимого (страница-глушка лимита
полна слов про капчу из подсказки), CF-челлендж — до всего остального (форма
тонет внутри challenge-страницы). Телефон — только пара «поле + слово
подтверждения»: `type="tel"` есть и в форме доставки живого магазина.
"""
import re
import sys

from http_.http_pool import http_pool_client

WEB_WALL_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

# (стена, маркеры, действие) — разбираются по порядку, первый всплеск решает
WEB_WALL_KINDS = (
    # interstitial Cloudflare: форма за ней недоступна вовсе
    ('challenge', (r'cf-chl', r'challenges\.cloudflare\.com', r'Just a moment',
                   r'challenge-platform', r'__cf_chl', r'cf_chl_opt'), 'human'),
    # виджеты капчи: recaptcha / hCaptcha / Turnstile / самописная картинка
    ('captcha', (r'g-recaptcha', r'google\.com/recaptcha', r'grecaptcha',
                 r'g-recaptcha-response', r'h-captcha', r'hcaptcha\.com',
                 r'cf-turnstile', r'<img[^>]{0,120}captcha', r'name="[^"]*captcha'),
     'human'),
    # верификация личности: паспорт/РНОКПП/Diia — автоматизма тут нет и быть не может
    ('identity', (r'IDENTITY_VERIFICATION', r'верификац\w*\s+(особи|личности)',
                  r'верифікаці\w*\s+особи', r'РНОКПП', r'diia\.', r'\bKYC\b',
                  r'identity[- ]verification'), 'human'),
    # блок/бан аккаунта или адреса
    ('blocked', (r'account.{0,40}(suspended|disabled|banned|blocked)',
                 r'заблокирован', r'заблоковано', r'access denied by',
                 r'cloudflare error 1020'), 'human'),
    # замок антибрутфорса: пара не проверена, нужен другой выход (web_login)
    ('lock', (r'error_attempts', r'too many (login |failed )?attempts',
              r'account.{0,30}locked', r'занадто багато спроб'), 'change_exit'),
)

# текст rate-limit: код может быть и 200, когда ответ объясняется словами
WEB_WALL_RATE_TEXT = re.compile(
    r'rate[ _-]?limit|RATE_LIMITED|слишком (много|часто)|попробуйте (позже|позднее)'
    r'|спробуйте пізніше|too many requests', re.I)
# слово подтверждения у телефонного поля: без него tel — просто телефон доставки
WEB_WALL_PHONE_ASK = re.compile(r'подтвержд|вериф|подтверд|підтверд|verify|verification|\bsms\b|смс|код', re.I)


def web_wall_scan(html: str = '', status=None, headers=None) -> dict:
    """Во что упёрся ответ: вердикт со стеной, действием и уликами.

    Args:
        html: тело ответа (для JS-рендеренной страницы — outerHTML из браузера).
        status: HTTP-код, если известен.
        headers: заголовки ответа (`Retry-After`, `cf-mitigated`).

    Returns:
        {'wall': ''|'challenge'|'captcha'|'identity'|'blocked'|'lock'|'phone'|'limit',
         'action': 'none'|'human'|'wait'|'change_exit',
         'wait': секунд до повторной попытки или None (окно назначает сайт),
         'evidence': [что именно нашлось]}
    """
    ev: list = []
    wait = None
    status = int(status) if status is not None and str(status).isdigit() else None
    head = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    # лимит — раньше содержимого: 429/Retry-After узнаются независимо от глушки
    if status == 429 or WEB_WALL_RATE_TEXT.search(html or ''):
        ra = head.get('retry-after', '')
        wait = int(ra) if ra.isdigit() else None
        ev.append('status 429' if status == 429 else 'rate-limit словами')
        if wait:
            ev.append(f'Retry-After={wait}')
        return {'wall': 'limit', 'action': 'wait', 'wait': wait, 'evidence': ev}

    # ответ Cloudflare может прилететь и без challenge-html — по заголовку
    if head.get('cf-mitigated', '').lower() == 'challenge':
        return {'wall': 'challenge', 'action': 'human', 'wait': None,
                'evidence': ['заголовок cf-mitigated: challenge']}

    for wall, markers, action in WEB_WALL_KINDS:
        for m in markers:
            if re.search(m, html or '', re.I):
                ev.append(f'{wall}: {m}')
                return {'wall': wall, 'action': action, 'wait': None, 'evidence': ev}

    if (re.search(r'type="tel"|name="[^"]*phone', html or '', re.I)
            and WEB_WALL_PHONE_ASK.search(html or '')):
        return {'wall': 'phone', 'action': 'human', 'wait': None,
                'evidence': ['поле телефона + слово подтверждения на странице']}

    if status == 451 or (status == 403
                        and re.search(r'blocked|заблокир|заблоковано|lawful', html or '', re.I)):
        return {'wall': 'blocked', 'action': 'human', 'wait': None,
                'evidence': [f'status {status} с текстом блока']}

    return {'wall': '', 'action': 'none', 'wait': None, 'evidence': []}


async def web_wall_probe(url: str, proxy: str = '', timeout: float = 15) -> dict:
    """Спросить живой адрес и вынести вердикт о стене — без входа и браузера.

    Точка для сторожа: «площадка вообще пускает?» до всякого входа. Ответ —
    fields `web_wall_scan` плюс `status` и `url` финальной точки.

    ⚠ Вердикт по голому GET честен для статических страниц; страницу, где
    капча вставляется JS после загрузки, увидит только outerHTML из браузера —
    туда же подавай тело в `web_wall_scan`.
    """
    async with http_pool_client(timeout=timeout, proxy=proxy,
                                follow_redirects=True,
                                headers={'User-Agent': WEB_WALL_UA}) as c:
        r = await c.get(url)
    verdict = web_wall_scan(r.text, status=r.status_code, headers=r.headers)
    verdict.update({'status': r.status_code, 'url': str(r.url)})
    return verdict


if __name__ == '__main__':
    import asyncio
    if len(sys.argv) < 2:
        print('употребление: python -m web_.web_wall URL [--proxy адрес]')
        raise SystemExit(2)
    args = sys.argv[1:]
    px = args[args.index('--proxy') + 1] if '--proxy' in args else ''
    print(asyncio.run(web_wall_probe(args[0], proxy=px)))

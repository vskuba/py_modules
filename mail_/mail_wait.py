"""Письмо-подтверждение: дождаться на ящике, вынуть ссылку, провести тем же выходом.

Регистрация на площадке часто заканчивается не `done`, а «проверьте почту»:
ссылка активационная живёт в письме, и её ещё надо поймать (письмо идёт минуты),
достать из тела (у `mail_box_letters` ссылки из introText, полноценное тело —
в сырой записи письма) и провести **тем же выходом**, что и регистрация: многие
площадки привязывают токен подтверждения к IP.

## Что здесь есть

* `mail_wait_letter` — дождаться письма на ящике и вынуть из него ссылки;
* `mail_wait_follow` — провести ссылку одним честным GET и вынести вердикт.

⚠ Ссылка подтверждения **одноразовая**: `mail_wait_follow` звать ровно один
раз, повтор — отказ площадки, а не повод долбить. Если письмо дошло до нас уже
сожжённым (сканер почты в mail.tm кликает ссылки сам) — не повторять, а идти
на площадку и просить «отправить письмо ещё раз».

⚠ Часть площадок по ссылке только **показывает** страницу с кнопкой «подтвердить»
(защита от сканеров почты). Вердикт `button` говорит об этом честно: дальше
только настоящий клик в браузере, автоматизма ссылкой тут нет.
"""
import asyncio
import re
import sys
import time

from http_.http_pool import http_pool_client
from mail_.mail_box import API, mail_box_letters, mail_box_links
from web_.web_wall import WEB_WALL_UA, web_wall_scan

# страница «нажми кнопку»: ссылка одна не завершает подтверждение
WEB_MAIL_BUTTON = re.compile(r'<(?:button|input[^>]*type="submit")[^>]*>'
                             r'|onclick[^>]*(confirm|verify)', re.I)
WEB_MAIL_ASK = re.compile(r'confirm|verify|подтверд|підтверд|активир', re.I)


async def mail_wait_letter(box: dict, contains: str = '', timeout: float = 180,
                           every: float = 4, seen=()) -> dict:
    """Дождаться письма на ящике: свежее, подходящее, со ссылками из полного тела.

    Args:
        box: {'login','token'} из `mail_box_new`.
        contains: подстрока от/темы/приветствия (домен площадки) — пустом ждём
            любое свежее письмо.
        timeout: секунд ожидания; площадка пишет и минуту, и пять.
        every: пауза между опросами ящика.
        seen: id прошлых писем — чтобы не принять вчерашнее за сегодняшнее.

    Returns:
        {'ok': bool, 'found': bool, 'id', 'from', 'subject', 'links': [...],
         'seen': [id для следующего захода], 'error': словами}
    """
    deadline = time.monotonic() + timeout
    read = set(seen)
    while time.monotonic() < deadline:
        got = await mail_box_letters(box, seen=read)
        if not got['ok']:
            return {'ok': False, 'found': False, 'id': '', 'from': '', 'subject': '',
                    'links': [], 'seen': sorted(read), 'error': got['error']}
        for letter in got['letters']:
            read.add(letter['id'])
            head = f"{letter['from']} {letter['subject']} {letter['intro']}"
            if contains and contains.lower() not in head.lower():
                continue
            links = list(letter['links'])
            raw = await _wait_raw(box, letter['id'])
            for part in raw:
                links += mail_box_links(part)
            return {'ok': True, 'found': True, 'id': letter['id'],
                    'from': letter['from'], 'subject': letter['subject'],
                    'links': list(dict.fromkeys(links)), 'seen': sorted(read), 'error': ''}
        await asyncio.sleep(every)
    return {'ok': True, 'found': False, 'id': '', 'from': '', 'subject': '',
            'links': [], 'seen': sorted(read),
            'error': f'письма нет за {int(timeout)} с — увеличь окно или сверь адрес'}


async def mail_wait_follow(url: str, proxy: str = '', timeout: float = 15) -> dict:
    """Провести ссылку подтверждения одним GET и вынести вердикт.

    Args:
        url: ссылка из письма.
        proxy: выход, тем же что и регистрация (токены привязывают к IP).

    Returns:
        {'ok': bool, 'verdict': 'done'|'button'|'wall'|'denied', 'status': код,
         'url': финальный, 'wall': словарь из `web_wall_scan`, 'error': словами}

    ⚠ Один вызов на ссылку: повторная попытка сожжёт её второй раз и вернёт
    уже отказ. Нужен второй заход — проси перевыпуск письма на площадке.
    """
    async with http_pool_client(timeout=timeout, proxy=proxy, follow_redirects=True,
                                headers={'User-Agent': WEB_WALL_UA}) as c:
        r = await c.get(url)
    wall = web_wall_scan(r.text, status=r.status_code, headers=r.headers)
    out = {'status': r.status_code, 'url': str(r.url), 'wall': wall}
    if wall['wall']:
        return {**out, 'ok': False, 'verdict': 'wall',
                'error': 'стена на подтверждении: ' + '; '.join(wall['evidence'])}
    if r.status_code >= 400:
        return {**out, 'ok': False, 'verdict': 'denied',
                'error': f'status {r.status_code} — возможно ссылка уже сожжена, '
                         'нужен перевыпуск письма'}
    if WEB_MAIL_BUTTON.search(r.text) and WEB_MAIL_ASK.search(r.text):
        return {**out, 'ok': False, 'verdict': 'button',
                'error': 'страница ждёт настоящего клика по кнопке — ссылкой не взять'}
    return {**out, 'ok': True, 'verdict': 'done', 'error': ''}


async def _wait_raw(box: dict, msg_id: str) -> list:
    """Полное тело письма: introText у списка неполный, ссылки лезут из тела."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f'{API}/messages/{msg_id}',
                            headers={'Authorization': f"Bearer {box['token']}"})
            js = r.json()
        html = js.get('html') or ''
        return [js.get('text') or '', html if isinstance(html, str) else ''.join(html)]
    except Exception:
        return []


if __name__ == '__main__':
    import asyncio
    a = sys.argv[1:]
    if len(a) < 2:
        print('употребление: python -m mail_.mail_wait wait ЛОГИН ТОКЕН'
              ' [--contains подстрока] [--timeout сек]')
        print('               python -m mail_.mail_wait follow ССЫЛКА [--proxy адрес]')
        raise SystemExit(2)
    kv = {a[i]: a[i + 1] for i in range(len(a) - 1) if a[i].startswith('--')}
    if a[0] == 'wait':
        print(asyncio.run(mail_wait_letter({'login': a[1], 'token': a[2]},
                                          contains=kv.get('--contains', ''),
                                          timeout=float(kv.get('--timeout', 180)))))
    else:
        print(asyncio.run(mail_wait_follow(a[1], proxy=kv.get('--proxy', ''))))

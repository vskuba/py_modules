"""Одноразовый почтовый ящик: завести, прочитать письмо, вытащить ссылку/токен.

Нужно войти на чужой сайт без своего ключа (регистрация, сброс пароля) — просится
ящик, который живёт минуты: завёл, получил письмо, забрал из него ссылку и забыл.
mail.tm отдаёт это API без ключа; здесь — ровно то же, что руками в curl, одним
вызовом и с разбором тела (ссылки и токены из письма выходят отдельно).

## Что здесь есть

* `mail_box_new` — завести ящик (адрес, пароль, токен);
* `mail_box_letters` — прочитать письма ящика;
* `mail_box_links` — вынуть из тела ссылки/токены (то, ради чего всё).

⚠ Бэкенд пока один (mail.tm); имя функции от него не зависит — второй бэкенд
ложится рядом, не трогая зовущих.
"""
import json
import re
import secrets

import httpx

API = 'https://api.mail.tm'


async def mail_box_new() -> dict:
    """Завести одноразовый ящик: адрес, пароль, токен.

    Returns:
        {'ok': bool, 'login': 'адрес', 'password': 'пароль', 'token': 'Bearer…',
         'error': причина словами (пусто при удаче)}
    """
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            dom = (await c.get(f'{API}/domains', params={'page': 1})).json()
            dom = (dom.get('hydra:member') or dom.get('member'))[0]['domain']
            login = 'agent-' + secrets.token_hex(3) + '@' + dom
            pwd = 'Xq7!' + secrets.token_hex(6)
            r = await c.post(f'{API}/token', json={'address': login, 'password': pwd})
            tok = r.json()['token'] if r.status_code == 200 else ''
            ok = r.status_code == 200
            return {'ok': ok, 'login': login, 'password': pwd, 'token': tok,
                    'error': '' if ok else 'почта-сервис вернул не токен'}
    except Exception as e:
        return {'ok': False, 'login': '', 'password': '', 'token': '',
                'error': f'{type(e).__name__}'}


async def mail_box_letters(box: dict, seen=()) -> dict:
    """Прочитать письма ящика; поле 'links' каждого — ссылки/токены из тела.

    Args:
        box: {'login','token'} из `mail_box_new`.
        seen: уже прочитанные id (чтобы следующий заход не повторял прошлые).

    Returns:
        {'ok': bool, 'letters': [{'id','from','subject','links','intro'}],
         'new': [id], 'error': …}
    """
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f'{API}/messages',
                            headers={'Authorization': f"Bearer {box['token']}"})
            js = r.json()
        items = js.get('hydra:member') or js.get('member') or []
        letters = [{'id': m.get('id'), 'from': str(m.get('from', {}).get('address', '')),
                    'subject': m.get('subject', ''),
                    'links': mail_box_links(m.get('introText', '') or ''),
                    'intro': (m.get('introText') or '')[:120]}
                   for m in items if m.get('id') not in set(seen)]
        return {'ok': True, 'letters': letters,
                'new': [l['id'] for l in letters], 'error': ''}
    except Exception as e:
        return {'ok': False, 'letters': [], 'new': [], 'error': f'{type(e).__name__}'}


def mail_box_links(text: str) -> list:
    """Вынуть из текста письма ссылки и токены (то, ради чего вход и сброс).

    Args:
        text: introText/тело письма.

    Returns:
        список найденных http(s)-ссылок и длинных токенов-подозрений.
    """
    urls = re.findall(r'https?://\S+', text)
    toks = re.findall(r'[A-Za-z0-9_-]{20,}', text)
    return list(dict.fromkeys(urls + toks))

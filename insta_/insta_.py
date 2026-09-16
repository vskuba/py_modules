"""Профиль Instagram как предмет путешествия: сначала изучить, потом качать только то, что зацепило.

Путешествие без браузера и без login: с 2024 Instagram анониму-краулеру отдаёт
только поларис-дамп **страницы профиля** под UA поисковика — карточку (имя,
био, счётчики подписчиков/подписок, is_private) и медиа с подписями постов.
Парсер дампа и скачивание — в `web_insta` (media, посты, реестр `posts.json`);
здесь только карточка, которой там не было, и «осмотр» целиком: посмотреть
профиль → по био и счётчикам решить, интересен ли он → `insta_pull` забрать
кадры: интересен — качаем, нет — идём дальше по списку URL.

Что за стеной — и не ждём:

* списки подписчиков и подписок анониму не отдаются (401 require_login,
  проверено прямым запросом) — видны только **числа**: `insta_followers` и
  `insta_following` честно возвращают счётчик, а не список имён;
* страницы тегов и постов краулеру — login-заглушка без медиа (проверено):
  путешествовать нужно списком ссылок на профили, а не графом ссылок;
* дамп отдаётся только crawler-UA (см. web_insta): обычный UA — стена.
"""
import argparse
import asyncio
import json
import re

from web_.web_insta import WEB_INSTA_UA, web_insta_download, web_insta_posts


async def insta_profile(url: str, proxy: str = '') -> dict:
    """Карточка профиля из дампа страницы: имя, био, счётчики, закрыт ли.

    Returns:
        dict: {username, full_name, biography, follower_count, following_count,
        is_private, pk} — поля, по которым решают «интересен ли профиль»;
        чего нет в дампе — пусто/0, а не выдумка.
    """
    html = await _page(url, proxy)
    # поля профиля лежат рядом с follower_count; окно вокруг них, чтобы не
    # схватить full_name первого попавшегося поста из этого же дампа
    i = html.find('"follower_count"')
    w = html[max(0, i - 3000):i + 3000] if i >= 0 else html

    def field(pattern: str, cast=str):
        m = re.search(pattern, w)
        if not m:
            return cast() if cast is not str else ''
        return cast(m.group(1)) if cast is not bool else m.group(1) == 'true'

    def text(raw: str) -> str:
        return json.loads(f'"{raw}"') if raw else ''

    return {
        'username': field(r'"username":"((?:[^"\\]|\\.)+)"'),
        'full_name': text(field(r'"full_name":"((?:[^"\\]|\\.)+)"')),
        'biography': text(field(r'"biography":"((?:[^"\\]|\\.)*)"')),
        'follower_count': field(r'"follower_count":(\d+)', int),
        'following_count': field(r'"following_count":(\d+)', int),
        'is_private': field(r'"is_private":(true|false)', bool),
        'pk': field(r'"pk":"(\d{5,})"'),
    }


async def insta_followers(url: str, proxy: str = '') -> dict:
    """Сколько у профиля подписчиков; список имён анониму не отдаётся (стена)."""
    card = await insta_profile(url, proxy)
    return {'username': card['username'], 'count': card['follower_count'],
            'users': [], 'wall': True}


async def insta_following(url: str, proxy: str = '') -> dict:
    """На сколько профилей подписан сам; имена подписок — тоже за стеной."""
    card = await insta_profile(url, proxy)
    return {'username': card['username'], 'count': card['following_count'],
            'users': [], 'wall': True}


async def insta_look(url: str, proxy: str = '', limit: int = 0) -> dict:
    """Осмотр профиля одним движением: карточка + что он выкладывает.

    limit — первые N медиа для пробы (0 — все). Медиа нести импортом из
    `web_insta`, не перепарсить здесь: второй парсер дампа разёлся бы с
    первым на первой же правке.
    """
    card = await insta_profile(url, proxy)
    media = await web_insta_posts(url)
    return {'profile': card, 'media_total': len(media),
            'media': media[:limit or None]}


async def insta_pull(url: str, out: str, proxy: str = '', limit: int = 0) -> list[dict]:
    """Что зацепило — забрать: медиа профиля в каталог out (как в web_insta)."""
    media = await web_insta_posts(url)
    return await web_insta_download(media[:limit or None], out)


async def _page(url: str, proxy: str) -> str:
    """Страница профиля глазами краулера: UA поисковика, прокси по желанию."""
    import httpx
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=proxy or None,
                                 headers={'User-Agent': WEB_INSTA_UA}) as c:
        r = await c.get(url if url.startswith('http') else
                        f'https://www.instagram.com/{url.strip("/")}/')
    r.raise_for_status()
    return r.text


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Профиль Instagram: осмотр (карточка, счётчики, медиа) и загрузка.')
    p.add_argument('url', help='профиль Instagram (ссылка или @имя)')
    p.add_argument('--out', default='', help='куда качать; пусто — только осмотр')
    p.add_argument('--limit', type=int, default=0, help='первые N медиа (0 — все)')
    p.add_argument('--proxy', default='', help='socks5://… если прямой выход упирается в лимит')
    ns = p.parse_args()
    if ns.out:
        rows = asyncio.run(insta_pull(ns.url, ns.out, ns.proxy, ns.limit))
        print(f'скачано: {len([r for r in rows if "err" not in r])}, '
              f'ошибок: {len([r for r in rows if "err" in r])}')
    else:
        view = asyncio.run(insta_look(ns.url, ns.proxy, ns.limit))
        print(json.dumps(view, ensure_ascii=False, indent=1))

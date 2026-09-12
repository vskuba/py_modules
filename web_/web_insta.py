"""Профиль Instagram как источник фото: список постов и загрузка — без браузера.

Instagram с 2024 не отдаёт анониму ни API, ни HTML без login-стены, зато
отдаёт полностью отрендеренный поларис-JSON crawler-у (Googlebot): в дампе
страницы каждый пост лежит объектом `XIGPolarisMedia` со своими
`image_versions2.candidates`. Отсюда весь приём: просим страницу под UA
поисковика, вырезаем из неё кандидатов, берём максимальный по пикселям.

Грабли, из-за которых это не однострочник:
* обычный UA получает 624 КБ login-заглушки без медиа вообще — только
  crawler-UA отдаёт поларис с кандидатами (признак удачи: `"taken_at"` в теле);
* `web_shot`/`web_probe` здесь не нужны: ссылки на оригиналы вшиты в дамп,
  браузер не поднимается вовсе; скриншот — фолбэк для сайтов, где ссылок нет;
* `\u0026` внутри JSON-строк дампа — амперсанды подписи URL: без замены на
  `&` подпись рвётся, CDN отвечает 403;
* ссылки подписаны по IP/UA и короткоживущие: скачал — значит сразу в файл,
  переиспользовать url позже нельзя;
* карусель: у каждого элемента свой `image_versions2`, а `pk` ближайшего поста
  один на всех выше по тексту — берём последний pk перед блоком и дедупим по
  нему, иначе дубликаты одного поста зальются пачкой.
"""
import argparse
import asyncio
import hashlib
import json
import re

# UA crawler'а: под ним Instagram отдаёт поларис-дамп с медиа вместо login-стены.
WEB_INSTA_UA = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'
WEB_INSTA_TIMEOUT = 30


async def web_insta_posts(url: str) -> list[dict]:
    """Список медиа профиля/поста: {pk, taken_at, url, w, h}.

    url — адрес профиля (https://www.instagram.com/<user>/) или поста. Пустой
    список — страница без поларис-дампа (стенка): смотреть web_shot.
    """
    import httpx
    async with httpx.AsyncClient(timeout=WEB_INSTA_TIMEOUT, follow_redirects=True,
                                headers={'User-Agent': WEB_INSTA_UA}) as c:
        r = await c.get(url if url.startswith('http') else
                        f'https://www.instagram.com/{url.strip("/")}/')
    r.raise_for_status()
    return _parse(r.text)


async def web_insta_download(posts: list[dict], out: str, concurrency: int = 6) -> list[dict]:
    """Скачать медиа постов в каталог out; вернуть строки манифеста.

    Имена — sha256[:32] содержимого (как везде в датасетах персоны), рядом
    ложится manifest.json со строками {file, pk, taken_at}. Уже скачанное по
    хэшу не перекачивается.
    """
    import httpx
    from pathlib import Path
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows, seen = [], _seen_rows(out)
    async with httpx.AsyncClient(timeout=WEB_INSTA_TIMEOUT, follow_redirects=True,
                                 headers={'User-Agent': WEB_INSTA_UA}) as c:
        sem = asyncio.Semaphore(concurrency)

        async def one(b):
            async with sem:
                try:
                    async with c.stream('GET', b['url']) as r:
                        if r.status_code != 200:
                            return {'err': f'HTTP {r.status_code}', 'pk': b['pk']}
                        body = b''
                        async for ch in r.aiter_bytes():
                            body += ch
                except Exception as e:
                    return {'err': type(e).__name__, 'pk': b['pk']}
                h = hashlib.sha256(body).hexdigest()[:32]
                if h in seen:
                    return None
                p = out / f'{h}.jpg'
                p.write_bytes(body)
                seen.add(h)
                return {'file': p.name, 'pk': b['pk'], 'taken_at': b['taken_at']}
        res = await asyncio.gather(*[one(b) for b in posts])
        rows = [r for r in res if r]
    if rows:
        old = out / 'manifest.json'
        prev = json.loads(old.read_text()) if old.exists() else []
        old.write_text(json.dumps(prev + rows, ensure_ascii=False, indent=1) + '\n')
    return rows


def _parse(html: str) -> list[dict]:
    """Выбрать из дампа каждый медиа один раз: ближайший pk до блока медиа."""
    out, seen = [], set()
    for m in re.finditer(r'"image_versions2":\{"candidates":', html):
        j = _balanced(html, m.end())
        try:
            cands = json.loads(html[m.end():j].replace('\\u0026', '&').replace('\\/', '/'))
        except (json.JSONDecodeError, ValueError):
            continue
        pk = re.findall(r'"pk":"(\d{5,})"', html[max(0, m.start() - 3000):m.start()])
        tk = re.findall(r'"taken_at":(\d+)', html[max(0, m.start() - 3000):m.start()])
        best = max(cands, key=lambda c: c.get('width', 0) * c.get('height', 0))
        key = pk[-1] if pk else best['url'][:64]
        if key in seen:
            continue
        seen.add(key)
        out.append({'pk': pk[-1] if pk else '', 'taken_at': int(tk[-1]) if tk else 0,
                    'url': json.loads(f'"{best["url"]}"'),
                    'w': best.get('width', 0), 'h': best.get('height', 0)})
    return out


def _balanced(s: str, i: int) -> int:
    """Индекс сразу за квадратным массивом, начинающимся в i."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '[':
            depth += 1
        elif s[j] == ']':
            depth -= 1
            if depth == 0:
                return j + 1
    raise ValueError('несбалансированный массив в дампе')


def _seen_rows(out) -> set:
    from pathlib import Path
    old = out / 'manifest.json'
    return {r['file'] for r in (json.loads(old.read_text()) if old.exists() else [])}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Фото из профиля Instagram: список или загрузка.')
    p.add_argument('url', help='профиль или пост Instagram')
    p.add_argument('--out', default='', help='каталог куда качать; пусто — только список')
    ns = p.parse_args()
    media = asyncio.run(web_insta_posts(ns.url))
    if ns.out:
        got = asyncio.run(web_insta_download(media, ns.out))
        print(f'скачано: {len([g for g in got if "err" not in g])}, '
              f'ошибок: {len([g for g in got if "err" in g])}')
    else:
        print(json.dumps(media, ensure_ascii=False, indent=1))

"""Полноразмерные фото страницы-галереи: список и загрузка — без браузера.

Анкеты и каталоги (ladadate и окрестности) отдают в HTML миниатюры с ленивой
разметкой: `data-src`/`data-full`/`data-original`/`srcset` лежат рядом с `src`,
а оригинал — тот же файл без суффикса миниатюры (`x-s.jpg` → `x.jpg`). Руками
это каждый раз одна и та же выжимка regex'ом с угадыванием суффикса; здесь —
один вызов.

Грабли, из-за которых это не один regex по `src`:
* `src` почти везде — миниатюра: оригинал живёт в `data-*` или в самой большой
  ветке `srcset`; смотреть надо все кандидаты тега, а не первый;
* суффиксы миниатюр (`-s`, `-thumb`, `-200x…`) у сайтов разные: оригинал
  выводится отжатием, и если бес-суффиксной ссылки в дампе нет — она УГАДАНА:
  качать надо с откатом на фактический вариант при 404;
* имена файлов на таких сайтах — уже хеш содержимого: их и сохраняем, коллизии
  редки и лечатся суффиксом `-1`;
* `srcset` перечисляет варианты через запятую с дескрипторами (`640w`) — берём
  адрес без дескриптора, дубликаты одного снимка сливаются по основанию имени.
"""
import argparse
import asyncio
import json
import re
from urllib.parse import urljoin

# атрибуты ленивой загрузки: оригнал там, `src` — обычно миниатюра
WEB_GALLERY_ATTRS = ('data-src', 'data-full', 'data-original', 'data-orig',
                    'data-lazy', 'src', 'srcset', 'data-srcset')
# суффикс миниатюры перед расширением: `-s`, `-thumb`, `-200x200`, `_small`, `@2x`
WEB_GALLERY_SUF = r'(?:[-_](?:s|tn|thumb|small|medium|\d{2,4}x\d{2,4})|@2x)$'
# служебная графика страницы: логотипы и заглушки lazy-загрузки в выборку не идут
WEB_GALLERY_SKIP = ('.svg', '.ico')
WEB_GALLERY_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
WEB_GALLERY_TIMEOUT = 30


async def web_gallery(url: str) -> list[dict]:
    """Фото страницы-галереи: [{'url': полноразмерный или угаданный оригинал,
    'fallback': фактический вариант из дампа, если оригинал угадан}, ...].

    Дубликаты одного фото (миниатюра + оригинал, `src` + `srcset`) сливаются по
    основанию имени; вариант без суффикса бьёт угаданный. Пустой список —
    страница без галереи.
    """
    import httpx
    async with httpx.AsyncClient(timeout=WEB_GALLERY_TIMEOUT,
                                 follow_redirects=True,
                                 headers={'User-Agent': WEB_GALLERY_UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
        html = r.text
    out: dict[str, dict] = {}
    for tag in re.findall(r'<img[^>]*>', html, re.I):
        attrs = dict(re.findall(
            r'\b(src|srcset|data-[a-z-]+)\s*=\s*"([^"]+)"', tag, re.I))
        cand = [x.strip().split(' ')[0] for v in attrs.values() for x in v.split(',')
                if '.' in x and not x.lower().endswith(WEB_GALLERY_SKIP)]
        for u in cand:
            orig, guess = _original(urljoin(url, u.split('?')[0]))
            key = orig.rsplit('/', 1)[-1]
            if key not in out or (out[key]['fallback'] and not guess):
                out[key] = {'url': orig, 'fallback': guess}
    return list(out.values())


async def web_gallery_download(items: list[dict], out: str,
                               concurrency: int = 6) -> list[dict]:
    """Скачать фото в каталог out; вернуть строки {file, url} (ошибки — {'err'}).

    Уже скачанное по имени не перекачивается, `gallery.json` дописывается.
    Угаданный оригинал проверяется делом: 404 — берём `fallback`, а не молча
    глохнем.
    """
    import httpx
    from pathlib import Path
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    old = out / 'gallery.json'
    prev = json.loads(old.read_text()) if old.exists() else []
    seen = {r['file'] for r in prev}

    async def one(c, b):
        name = b['url'].rsplit('/', 1)[-1]
        if name in seen:
            return None
        urls = [b['url']] + ([b['fallback']] if b['fallback'] else [])
        for u in urls:
            try:
                r = await c.get(u)
                if r.status_code != 200:
                    continue
                stem = name.rsplit('.', 1)[0]
                ext = name.rsplit('.', 1)[1]
                final = name if name not in seen else f'{stem}-{len(seen)}.{ext}'
                (out / final).write_bytes(r.content)
                seen.add(final)
                return {'file': final, 'url': u}
            except Exception:
                continue
        return {'err': name}

    async with httpx.AsyncClient(timeout=WEB_GALLERY_TIMEOUT,
                                 follow_redirects=True,
                                 headers={'User-Agent': WEB_GALLERY_UA}) as c:
        sem = asyncio.Semaphore(concurrency)

        async def gated(b):
            async with sem:
                return await one(c, b)
        res = await asyncio.gather(*[gated(b) for b in items])
    rows = [r for r in res if r and 'err' not in r]
    errs = [{'err': r['err']} for r in res if r and 'err' in r]
    if rows:
        old.write_text(json.dumps(prev + rows, ensure_ascii=False, indent=1) + '\n')
    return rows + errs


# ── детали реализации ──

def _original(url: str) -> tuple[str, str]:
    """(оригинал, угадка): имя без суффикса миниатюры; угадка — сам url, если
    бес-суффиксной ссылки в дампе не было (суффикс отжат, оригинал угадан)."""
    name = url.rsplit('/', 1)[-1]
    stem, ext = name.rsplit('.', 1)[0], name.rsplit('.', 1)[1]
    base = re.sub(WEB_GALLERY_SUF, '', stem)
    orig = url.rsplit('/', 1)[0] + '/' + base + '.' + ext
    return orig, ('' if base == stem else url)


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Фото из страницы-галереи: полноразмерные ссылки (миниатюры '
                    'раскручиваются до оригиналов) и закачка в каталог.')
    p.add_argument('url', help='страница с галереей')
    p.add_argument('--out', default='', help='каталог куда качать; пусто — список')
    p.add_argument('--limit', type=int, default=0, help='не более N фото')
    ns = p.parse_args()
    media = asyncio.run(web_gallery(ns.url))
    if ns.limit:
        media = media[:ns.limit]
    if ns.out:
        got = asyncio.run(web_gallery_download(media, ns.out))
        print(f'скачано: {len([g for g in got if "err" not in g])}, '
              f'ошибок: {len([g for g in got if "err" in g])}')
    else:
        print(json.dumps(media, ensure_ascii=False, indent=1))

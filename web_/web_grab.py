"""Пачка ссылок → файлы на диске: тип по голове, а не по расширению, UA как у браузера.

Лицо качали с стоковой галереи — половина «.jpg» оказалась Cloudflare-страницей:
`is_file()` у таких огрызков истинен, и трейнер потом молча учился на HTML. Этот
модуль качает так, как отдаёт: содержимое сверяется с головой (`file_check`),
страница вместо файла — честный отказ словами, а не молчаливый `.jpg` с тегом
`<html>` внутри; userAgent — браузерный, иначе сайты отдают заглушку; JSON-ответ
API разворачивается по точечному пути в обычные ссылки.
"""
import httpx

from file_.file_check import FILE_CHECK_MAGIC, file_check

# Заглушки CDN на «curl-подобный» UA отвечают страницей вместо файла — отсюда
# настоящий браузерный UA по умолчанию (грань та же, что у `web_peek` с маркерами).
WEB_GRAB_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def web_grab(urls, to='.', *, ua=WEB_GRAB_UA, path='') -> list:
    """Скачать пачку ссылок честными файлами: тип по голове, страница — отказ.

    Args:
        urls: адрес или список адресов.
        to: каталог, куда писать файлы (создаётся).
        ua: User-Agent запроса; по умолчанию браузерный — с заглушатым сайты
            выдают HTML вместо файла, и это потом не отличить от поломки.
        path: точечный путь внутри JSON-ответа (`query.pages.*.imageinfo.0.thumburl`);
            JSON без пути наружу не разворачивается и качается как файл.

    Returns:
        список {'address', 'file', 'type', 'bytes', 'why'} на каждый скачанный
        адрес: `type` — угадан по голове (jpg/png/webp/json/…), `why` пусто,
        когда пришёл настоящий файл; страница вместо файла — тот же список, но
        с `file`: '' и `why`: 'страница вместо файла…' — молча сохранять её
        под расширением картинки нельзя.

    ⚠ Сверяется голова с расширением уже скачанного (`file_check`): если сайт
    отдал JSON-ошибку под видом картинки — это будет видно по `why`, а не
    молчаливым «цел».
    """
    import json as _json
    from pathlib import Path
    from urllib.parse import unquote, urlsplit

    Path(to).mkdir(parents=True, exist_ok=True)
    out: list = []
    with httpx.Client(headers={'User-Agent': ua}, follow_redirects=True,
                      timeout=30) as c:
        queue = [u for u in ([urls] if isinstance(urls, str) else list(urls))]
        while queue:
            u = queue.pop(0)
            data = c.get(u).content
            head = data.lstrip()[:1]
            if path and head in (b'{', b'['):
                links = _cut(_json.loads(data), path.split('.'))
                queue.extend(x for x in (links if isinstance(links, list)
                                         else [links]) if isinstance(x, str))
                continue
            typ = _sniff(data)
            name = unquote(urlsplit(u).path).rsplit('/', 1)[-1]
            entry = {'address': u, 'file': '', 'type': typ or 'не опознан',
                     'bytes': len(data), 'why': ''}
            if head == b'<':
                entry['type'] = 'html'
                entry['why'] = ('страница вместо файла (сайт отрезал UA? '
                                   'url не про файл?)')
            else:
                if not Path(name).suffix and typ:
                    name += '.' + typ
                p = Path(to) / name
                p.write_bytes(data)
                got = file_check(p)
                if not got['intact']:
                    p.unlink(missing_ok=True)
                    entry['why'] = got['why']
                else:
                    entry['file'], entry['type'] = str(p), typ or entry['type']
            out.append(entry)
    return out


# ── детали реализации ──

def _sniff(data: bytes) -> str:
    """Настоящий тип по магической голове ('jpg', 'png', 'json', …); неизвестно — ''."""
    for ext, magic in FILE_CHECK_MAGIC.items():
        if data.startswith(magic) and not (
                ext == 'webp' and data[8:12] != b'WEBP'):
            return 'jpg' if ext == 'jpeg' else ext
    return {'{': 'json', '[': 'json'}.get(data.lstrip()[:1].decode(), '')


def _cut(v, segs: list):
    """Точечный путь внутри JSON-ответа; '*' — пройтись по списку ИЛИ по
    значениям словаря (у MediaWiki `pages` ключи — id страниц)."""
    if not segs:
        return v
    s, rest = segs[0], segs[1:]
    if s == '*':
        return [_cut(x, rest) for x in (v.values() if isinstance(v, dict) else v)]
    return _cut(v[int(s)] if isinstance(v, list) else v[s], rest)


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='Пачка ссылок в честные файлы: тип по голове, UA браузерный, '
                    'страница вместо файла — отказ словами.')
    ap.add_argument('urls', nargs='+', help='адреса')
    ap.add_argument('--to', default='.', help='каталог назначения')
    ap.add_argument('--ua', default=WEB_GRAB_UA, help='User-Agent запроса')
    ap.add_argument('--path', default='',
                    help='точечный путь в JSON-ответе, ведущий к ссылкам')
    ns = ap.parse_args()
    print(json.dumps(web_grab(ns.urls, to=ns.to, ua=ns.ua, path=ns.path),
                     ensure_ascii=False, indent=1))

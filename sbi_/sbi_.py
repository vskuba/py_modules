"""
Поиск по фото: «где ещё эта картинка в сети» без аккаунтов.

Движки реверс-поиска не имеют API для анонимов, но имеют мышеловки для всех:
каждая ссылка поиска принимает **картинку по URL**, а не только загрузку из
формы — этим мышеловкой и пользуются. Файл предварительно залетает на
файлодержатель (`sbi_host`, catbox — без входа и ключей), дальше по каждой
ссылке идёт браузер-контейнер и читает выдачу как страницу.

Что движок умеет ответить анонимно — разное, и инструмент честно это
различает в `status`:

* `bing` — `cbir=sbi&imgurl=`: отдаёт кластеры «похожих» с доменами; по
  своему лицу находит плохо (общие темы вместо персоны), по товару — годно;
* `yandex` — `rpt=imageview&url=`: упрямо требует, чтобы адрес картинки
  открылся без редиректа и с content-type `image/*`; отдаёт «не найдено»
  честным текстом, не капчей;
* `lens` — `lens.google.com/uploadbyurl?url=`: самый упрямый по лицам, но
  с датацентровых адресов и прокси-redcarpet уходит на `/sorry` (капча
  человека) — это `blocked`, а не «ничего нет»; молча путать их — значит
  похоронить зацепку.

Правило прочтения: пустая выдача и непроход — **разные ответы**. `nothing`
закрывает направление, `blocked` зовёт сменить адрес выхода (`web_proxy`
проверит годность тем же путём). Движки, требующие JS-загрузчик формы
(facecheck и подобные), сюда не зовутся: контейнер не исполняет произвольный
JS — их направление это не чинит.

Родственное: `browser_http` (транспорт), `image_body` (паспорт фигуры, когда
движок глух), `web_probe` (та же страница без браузера).
"""

import argparse
import secrets
import urllib.parse
import urllib.request

from browser_.browser_http import (browser_http_close, browser_http_goto,
                                   browser_http_open, browser_http_wait)

SBI_HOST_URL = 'https://catbox.moe/user/api.php'
SBI_ENGINES = ('bing', 'yandex', 'lens')

# Что в тексте означает «капча/не пускают», а что — «пусто». Пустой ответ
# каждого движка своими словами; `Find an image: пусто` — вердикт Яндекса.
SBI_BLOCKED = ('/sorry', 'captcha', 'unusual traffic', 'Подтвердите, что запросы')
SBI_EMPTY = ('no results', 'did not match', 'не найдено', 'ничего не найдено',
             'не удалось найти', 'find an image: пусто')

# Соцсети, чьи ссылки ищем в выдаче: ради них инструмент и заводится.
SBI_SITES = ('facebook.com', 'instagram.com', 'linkedin.com', 'tiktok.com',
             'x.com', 'twitter.com', 'ok.ru', 'vk.com')


def sbi_host(src) -> str:
    """Файл на файлодержателе, ссылка наружу (или '' — держатель молчит).

    Нужна именно ссылка: все анонимные ручки реверс-поиска принимают картинку
    **по URL**. Загрузка формы в контейнере недоступна —multipart-заговор здесь
    единственный путь. Кошка (catbox) хранит вечно и без ключа; свой хост
    предпочтительнее там, где он есть.
    """
    from pathlib import Path

    body = (f'--x\r\nContent-Disposition: form-data; name="reqtype"\r\n\r\n'
            f'fileupload\r\n--x\r\nContent-Disposition: form-data; '
            f'name="fileToUpload"; filename="{Path(src).name}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n')
    body = body.encode() + Path(src).read_bytes() + b'\r\n--x--\r\n'
    req = urllib.request.Request(SBI_HOST_URL, data=body, headers={
        'Content-Type': 'multipart/form-data; boundary=x',
        'User-Agent': 'Mozilla/5.0'})
    try:
        return urllib.request.urlopen(req, timeout=60).read().decode().strip()
    except Exception:
        return ''


def sbi_search(src, engines: tuple = SBI_ENGINES, browser: str = '') -> dict:
    """Картинка в каждый движок: `{движок: {status, note, links}}`.

    `src` — файл (залетит на держатель) или уже ссылка. `status`: `ok` — есть
    выдача, `nothing` — движок сказал «пусто», `blocked` — капча (менять
    адрес выхода, не направление), `error` — не спросили. `links` — ссылки
    соцсетей из выдачи, только они: мусор агрегаторов решению не помогает.
    """
    url = src if str(src).startswith('http') else sbi_host(src)
    if not url:
        return {e: {'status': 'error', 'note': 'картинку не удалось выложить'}
                for e in engines}

    return {e: _engine(e, url, browser) for e in engines}


def _engine(engine: str, url: str, browser: str) -> dict:
    addr = {'bing': f'https://www.bing.com/images/search?view=detailv2&iss=sbi'
                    f'&form=SBIVSP&sbisrc=UrlPasteBox&q=imgurl:{url}',
            'yandex': 'https://yandex.com/images/search?rpt=imageview&url='
                      + urllib.parse.quote(url, safe=''),
            'lens': 'https://lens.google.com/uploadbyurl?url='
                    + urllib.parse.quote(url, safe='')}[engine]

    sid = browser_http_open(f'sbi-{engine}-{secrets.token_hex(3)}', '', browser)
    try:
        browser_http_goto(sid, addr, 'domcontentloaded', 90000)
        page = browser_http_wait(sid, 'Image', tries=6)
        sites = [l['href'] for l in (page.get('links') or [])
                 if any(t in (l.get('href') or '') for t in SBI_SITES)][:20]
    except Exception as e:
        return {'status': 'error', 'note': str(e)[:120], 'links': []}
    finally:
        browser_http_close(sid)

    text = ((page or {}).get('text') or '').lower()
    if not text:
        return {'status': 'error', 'note': 'страница не прочиталась', 'links': []}
    blocked = next((m for m in SBI_BLOCKED if m in text), '')
    if blocked:
        return {'status': 'blocked', 'note': blocked, 'links': []}

    # Заголовок-узнавание ('Image appears to contain', название фильма) и есть
    # ответ: он важнее ссылок, которых у анонимной выдачи почти нет.
    topic = next((l[3:] for l in ((page or {}).get('text') or '').splitlines()
                  if l.startswith('## ') and not l[3:].startswith('Image appears')), '')
    empty = next((m for m in SBI_EMPTY if m in text), '')
    if empty and not (sites or topic):
        return {'status': 'nothing', 'note': empty, 'links': []}

    return {'status': 'ok', 'note': (topic or empty or 'выдача есть')[:100],
            'links': sites}


def sbi_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='реверс-поиск по картинке')
    parser.add_argument('src', help='файл или ссылка на картинку')
    parser.add_argument('--engines', default=','.join(SBI_ENGINES))
    parser.add_argument('--browser', default='', help='адрес контейнера')
    parser.add_argument('--host-only', action='store_true',
                        help='только выложить на держатель')
    args = parser.parse_args(argv)

    if args.host_only:
        print(sbi_host(args.src) or 'держатель молчит')
        return 0

    for eng, r in sbi_search(args.src, tuple(args.engines.split(',')),
                             args.browser).items():
        print(f'{eng:8} {r["status"]:8} {r["note"]}')
        for l in r['links']:
            print(f'         {l}')

    return 0


if __name__ == '__main__':
    raise SystemExit(sbi_main())

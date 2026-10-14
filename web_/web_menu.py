"""
Разделы сайта из его навигации: подпись и адрес, без браузера.

Вопрос «какие разделы есть у этого сайта» задают постоянно — обход каталога,
сбор карты, проверка после выкладки, — и каждый раз отвечают заново: качают
страницу, вытягивают `<a>`, глазами отделяют разделы от футера и шапки. Работа
одна и та же, а эвристика у всех своя, и у каждой свои дыры.

Разбор идёт по готовому HTML, а не по живой странице: качать умеет `web_peek`,
и второй качалке внутри делать нечего. Строчное дело — отделить навигацию от
всего остального.

## ⚠ Что считается навигацией

Ссылки внутри `<nav>`, `<ul>` с «menu»/«nav» в классе или id, и `<header>`.
Этого хватает подавляющему большинству сайтов — меню верстают именно так уже
лет пятнадцать. Чего в этих блоках нет, того функция не вернёт: лучше честный
недобор, чем список, в котором половина — ссылки из подвала.

## ⚠⚠ Подпись — то, что видит человек, а не `title` и не кусок адреса

Текст собирается из всего содержимого ссылки, вместе с вложенными тегами, и
схлопывается по пробелам. Иконочные ссылки (пустой текст) выбрасываются: это
корзина и поиск, а не разделы. Длинные (`text_max`) — тоже: так верстают
баннеры внутри меню, и «Ноутбуки без зайвої націнки Перевір нашу добірку» —
не название раздела.

## ⚠ Один раздел — одна запись

Меню повторяет себя: десктопное и мобильное, шапка и мегаменю. Ссылки
схлопываются по подписи, первая выигрывает — она из главного меню, оно идёт
в разметке раньше.
"""
import argparse
import asyncio
import html as html_mod
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

if __package__ in (None, ''):
    _here = str(Path(__file__).resolve().parent)
    sys.path[:] = [item for item in sys.path if item not in ('', '.', _here)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Блоки, внутри которых ищем ссылки. Больше пятнадцати лет меню верстают так, и
# список закрыт намеренно: открытый («любой `ul`») тянет подвал и колонку
# товаров, а чистить результат потом дороже, чем недобрать здесь.
_MENU_BLOCKS = (
    r'<nav\b[^>]*>(.*?)</nav>',
    r'<header\b[^>]*>(.*?)</header>',
    r'<ul\b[^>]*(?:class|id)="[^"]*(?:menu|nav)[^"]*"[^>]*>(.*?)</ul>',
    r'<div\b[^>]*(?:class|id)="[^"]*(?:menu|nav)[^"]*"[^>]*>(.*?)</div>',
)

_LINK = re.compile(r'<a\b[^>]*\bhref="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_TAG = re.compile(r'<[^>]+>')
_SPACE = re.compile(r'\s+')

# Чего в разделах не бывает. Служебные адреса (`mailto:`, `tel:`, `#`) и
# якоря — это связь и переключатели, а не части сайта.
_SKIP_PREFIX = ('#', 'javascript:', 'mailto:', 'tel:', 'viber:', 'data:')


def web_menu_links(html: str, base_url: str = '', text_max: int = 60) -> list:
    """Разделы из навигации страницы: `[{'text': …, 'url': …}, …]`.

    Args:
        html: тело страницы как есть — например, из `web_peek(...)['text']`.
        base_url: адрес, относительно которого достраиваются ссылки. Пусто —
            адреса возвращаются как в разметке.
        text_max: длиннее этого подпись считается баннером, а не разделом.

    Порядок — порядок в разметке: он и есть порядок меню, задуманный автором
    сайта. Сортировать по алфавиту значило бы потерять это знание.
    """
    out, seen = [], set()

    for inner in _menu_areas(html or ''):
        for href, body in _LINK.findall(inner):
            text = _text_of(body)
            if not text or len(text) > int(text_max):
                continue
            if text.casefold() in seen:
                continue
            url = html_mod.unescape((href or '').strip())
            if not url or url.startswith(_SKIP_PREFIX):
                continue

            seen.add(text.casefold())
            out.append({'text': text,
                        'url': urljoin(base_url, url) if base_url else url})

    return out


async def web_menu_fetch(url: str, text_max: int = 60, timeout: float = 15) -> list:
    """То же, но страницу качает сам — одной строкой на весь вопрос.

    Raises:
        RuntimeError: сайт ответил не страницей (код не 2xx или пустое тело).
    """
    from web_.web_peek import web_peek

    answer = await web_peek(url, timeout=timeout)
    code = int(answer.get('status') or 0)
    body = answer.get('text') or ''
    if not 200 <= code < 300 or not body:
        raise RuntimeError(f'{url} ответил {code or "молчанием"}')

    return web_menu_links(body, url, text_max)


# ── детали реализации ──────────────────────────────────────────────────────

def _menu_areas(html: str) -> list:
    """Куски разметки, внутри которых имеет смысл искать разделы.

    ⚠ Блоки могут вкладываться друг в друга (`nav` внутри `header`), и одна и
    та же ссылка попадётся дважды. Это не беда: схлопывание по подписи выше
    оставит первую, а она из блока, который стоит в разметке раньше.
    """
    areas = []
    for pattern in _MENU_BLOCKS:
        areas.extend(re.findall(pattern, html, re.S | re.I))

    return areas


def _text_of(body: str) -> str:
    """Подпись ссылки: всё видимое внутри неё, схлопнутое по пробелам."""
    return _SPACE.sub(' ', html_mod.unescape(_TAG.sub(' ', body))).strip()


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog='python -m web_.web_menu',
        description='Разделы сайта из его навигации: подпись и адрес.')
    parser.add_argument('url', help='адрес страницы')
    parser.add_argument('--text-max', type=int, default=60,
                        help='длиннее этого подпись считается баннером')
    parser.add_argument('--json', action='store_true', help='вывод JSON')
    args = parser.parse_args()

    try:
        rows = asyncio.run(web_menu_fetch(args.url, args.text_max))
    except Exception as fault:
        print(f'ошибка: {fault}', file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f'{row["text"]:40} {row["url"]}')
        print(f'— разделов: {len(rows)}')

    return 0


if __name__ == '__main__':
    raise SystemExit(_main())

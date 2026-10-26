"""Энциклопедия в сверке: Википедия там, где обычный запрос получает 403.

Сверить вывеску с настоящим владельцем бренда (чей «Ariete» — чайники, чей —
очки), найти дату сделки по слиянию, поймать омонимию торговых имён — почти
всё это уже написано в Википедии, но fetch'у с браузерным UA она отвечает
403: фонд просит представиться. Отсюда и инструмент: тот же запрос, но с
UA-представлением, плюс два API — REST-выжимка и полный текст страницы.

## Что здесь есть

* `osint_wiki_summary` — выжимка (первый абзац + URL) через REST API;
* `osint_wiki_text` — текст страницы (весь или только вступление) через
  action API, plain text.

⚠ Перенаправление follows=1: `Ariete` попадёт на страницу разрешения
неоднозначности — смотри `title` в ответе и идуй по ссылке точнее.
⚠ Википедия — свидетель, а не арбитр: для факта в отчёт идёт ссылка на
статью, а не пересказ.
"""
import argparse
import urllib.parse

import httpx

# Фонд просит UA с контактом; браузерный UA их WAF считает ботом и даёт 403.
OSINT_WIKI_UA = {'User-Agent': 'research-script/1.0 (contact: intel@example.com)'}


def osint_wiki_summary(lang: str, title: str, *, timeout: float = 15.0) -> dict:
    """Выжимка статьи: заголовок, описание, первый абзац, ссылка.

    Args:
        lang: поддомен языка (`it`, `ru`, `en`, …).
        title: название статьи (кавычки и пробелы understood — URL-кодируются).
        timeout: секунд на запрос.

    Returns:
        {'ok': bool, 'title': '…', 'extract': '…', 'url': '…',
         'error': словами (пусто при удаче)}
    """
    q = urllib.parse.quote(title.replace(' ', '_'), safe='()_-,')
    try:
        r = httpx.get(f'https://{lang}.wikipedia.org/api/rest_v1/page/summary/{q}',
                      headers=OSINT_WIKI_UA, follow_redirects=True,
                      timeout=timeout)
        js = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {'ok': False, 'title': title, 'extract': '', 'url': '',
                'error': f'{type(e).__name__}'}
    return {'ok': r.status_code == 200 and 'extract' in js,
            'title': js.get('title', title),
            'extract': js.get('extract', ''),
            'url': (js.get('content_urls', {}).get('desktop', {}) or {}).get('page', ''),
            'error': '' if r.status_code == 200 else f'HTTP {r.status_code}'}


def osint_wiki_text(lang: str, title: str, *, intro: bool = True,
                    timeout: float = 20.0) -> dict:
    """Текст статьи plain text: вступление (`intro`) или целиком.

    Args:
        lang: поддомен языка.
        title: название статьи.
        intro: True — только первый абзац; False — всю страницу.
        timeout: секунд на запрос.

    Returns:
        {'ok': bool, 'title': '…', 'text': '…', 'error': словами}
    """
    # exintro — признак «только вступление»; без него API отдаёт страницу целиком.
    params = {'action': 'query', 'format': 'json', 'redirects': '1',
              'prop': 'extracts', 'explaintext': '1', 'titles': title,
              **({'exintro': '1'} if intro else {})}
    try:
        r = httpx.get(f'https://{lang}.wikipedia.org/w/api.php', params=params,
                      headers=OSINT_WIKI_UA, timeout=timeout)
        js = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {'ok': False, 'title': title, 'text': '',
                'error': f'{type(e).__name__}'}
    pages = (js.get('query', {}).get('pages') or {})
    for p in pages.values():
        if p.get('extract'):
            return {'ok': True, 'title': p.get('title', title),
                    'text': p['extract'], 'error': ''}
    return {'ok': False, 'title': title, 'text': '', 'error': 'пустая выжимка'}


if __name__ == '__main__':
    _P = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _P.add_argument('lang')
    _P.add_argument('title')
    _P.add_argument('--full', action='store_true', help='весь текст, не вступление')
    _a = _P.parse_args()
    if _a.full:
        _res = osint_wiki_text(_a.lang, _a.title, intro=False)
        print(_res['title'], _res['error'])
        print(_res['text'])
    else:
        _res = osint_wiki_summary(_a.lang, _a.title)
        print(_res['title'], _res['url'], _res['error'])
        print(_res['extract'])
    raise SystemExit(0 if _res.get('ok') else 1)

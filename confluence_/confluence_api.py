"""
Confluence Cloud (REST v2): страница текстом, дерево пространства, выгрузка, поиск CQL.

Родился, чтобы пройти пространство CAS (Cashier) целиком и сверить с ним
локальную документацию: в браузере 200 страниц не прочитать, а API отдаёт
тело в storage-формате (XHTML с макросами `ac:`), который глазами не читается.

## Доступ (проверено на protonixltd.atlassian.net/wiki, 2026-11)

Тот же сайт и тот же токен, что у Jira: `JIRA_URL`, `JIRA_EMAIL`,
`JIRA_API_TOKEN` из `.env.local`, запросы — `jira_api_request(...,
product='confluence')` (прямой адрес `{JIRA_URL}/wiki/...`, на 401 — шлюз
`api.atlassian.com/ex/confluence/{cloudId}`). Классический токен (без
scopes) Confluence видит сразу; scoped — только с confluence-scopes.

* **Номер в ссылке — это id страницы**: `/wiki/spaces/CAS/pages/1019904001/…`
  → `1019904001`; `confluence_api_page_id` вынимает его из ссылки.
* **Список страниц — `/spaces/{id}/pages`, а id пространства — не ключ**:
  `CAS` переводится в числовой id запросом `/spaces?keys=CAS`.
* **Пагинация — `_links.next`** (курсор), относительный путь от сайта.
* **Тело — `body-format=storage`**: таблицы, макросы кода (`ac:structured-macro
  ac:name="code"`), ссылки на страницы (`ac:link` → `ri:page`). Перевод в
  markdown-подобный текст — `confluence_api_storage_text`; вложения и
  картинки становятся пометкой `[вложение: имя]`.
* Поиск — v1 `rest/api/search?cql=` (в v2 CQL нет).

## Что здесь есть

* `confluence_api_page_id` — id из ссылки или строки.
* `confluence_api_space` — пространство по ключу (`id`, `name`).
* `confluence_api_pages` — все текущие страницы пространства (id, title, parentId, версия).
* `confluence_api_page` / `confluence_api_page_text` — страница сырым JSON / текстом.
* `confluence_api_tree` — дерево страниц отступами.
* `confluence_api_export` — всё пространство в каталог: `<id>.md` + `index.tsv`.
* `confluence_api_search` — CQL-поиск.
* `confluence_api_storage_text` — storage XHTML → текст.

CLI:

    python -m confluence_.confluence_api page https://…/wiki/spaces/CAS/pages/1019904001/…
    python -m confluence_.confluence_api tree CAS
    python -m confluence_.confluence_api export CAS /tmp/cas
    python -m confluence_.confluence_api search 'space = CAS AND text ~ "payout"'

`--root` — каталог проекта с `.env.local` (по умолчанию текущий).
"""
import argparse
import json
import os
import re
import sys
import urllib.parse

from bs4 import BeautifulSoup, NavigableString, Tag

from jira_.jira_api import jira_api_env, jira_api_request


def confluence_api_page_id(text: str) -> str:
    """id страницы из ссылки `…/pages/<id>/…`, `?pageId=<id>` или голого числа.

    Raises:
        ValueError: числа в строке нет.
    """
    match = re.search(r'/pages/(\d+)', text) or re.search(r'pageId=(\d+)', text) \
        or re.fullmatch(r'\s*(\d+)\s*', text)
    if not match:
        raise ValueError(f'в «{text}» нет id страницы Confluence')
    return match.group(1)


def confluence_api_space(key: str, env: dict | None = None) -> dict:
    """Пространство по ключу (`CAS`): `id`, `key`, `name`, `homepageId`.

    Raises:
        RuntimeError: пространства нет или нет прав.
    """
    found = _confluence_get('wiki/api/v2/spaces', {'keys': key}, env).get('results', [])
    if not found:
        raise RuntimeError(f'пространство {key} не найдено или недоступно')
    return found[0]


def confluence_api_pages(space_key: str, env: dict | None = None) -> list:
    """Все текущие страницы пространства: `id`, `title`, `parentId`, `version`, `updated`."""
    space = confluence_api_space(space_key, env)
    pages, path, params = [], f"wiki/api/v2/spaces/{space['id']}/pages", {'limit': 250, 'status': 'current'}
    while path:
        page = _confluence_get(path, params, env)
        for item in page.get('results', []):
            pages.append({'id': item['id'], 'title': item['title'], 'parentId': item.get('parentId'),
                          'version': item.get('version', {}).get('number'),
                          'updated': item.get('version', {}).get('createdAt', '')})
        path, params = _confluence_next(page)
    return pages


def confluence_api_page(page: str, env: dict | None = None) -> dict:
    """Страница сырым JSON с телом в storage-формате; `page` — id или ссылка."""
    return _confluence_get(f'wiki/api/v2/pages/{confluence_api_page_id(page)}',
                           {'body-format': 'storage'}, env)


def confluence_api_page_text(page: dict, base_url: str = '') -> str:
    """Страница текстом: заголовок, версия, ссылка, тело."""
    head = [f"# {page['title']}",
            f"id {page['id']} · версия {page.get('version', {}).get('number')} · "
            f"{page.get('version', {}).get('createdAt', '')[:10]}"]
    if base_url:
        head.append(f"{base_url}/wiki/pages/viewpage.action?pageId={page['id']}")
    body = page.get('body', {}).get('storage', {}).get('value', '')
    return '\n'.join(head) + '\n\n' + confluence_api_storage_text(body)


def confluence_api_tree(pages: list) -> str:
    """Дерево страниц отступами (по `parentId`); сироты — от корня."""
    ids = {p['id'] for p in pages}
    children: dict = {}
    for p in pages:
        parent = p['parentId'] if p['parentId'] in ids else None
        children.setdefault(parent, []).append(p)
    lines = []

    def walk(parent, depth):
        for p in sorted(children.get(parent, []), key=lambda x: x['title'].lower()):
            lines.append(f"{'  ' * depth}{p['title']}  [{p['id']}]")
            walk(p['id'], depth + 1)

    walk(None, 0)
    return '\n'.join(lines)


def confluence_api_export(space_key: str, out_dir: str, env: dict | None = None) -> list:
    """Всё пространство в каталог: `<id>.md` на страницу, `index.tsv` (id, parent, версия, дата, путь заголовков).

    Возвращает список страниц (как `confluence_api_pages`) с полем `path`.
    """
    env = env or jira_api_env()
    os.makedirs(out_dir, exist_ok=True)
    pages = confluence_api_pages(space_key, env)
    by_id = {p['id']: p for p in pages}
    for p in pages:
        chain, cur = [], p
        while cur:
            chain.append(cur['title'])
            cur = by_id.get(cur['parentId'])
        p['path'] = ' / '.join(reversed(chain))
        text = confluence_api_page_text(confluence_api_page(p['id'], env), env['url'])
        with open(os.path.join(out_dir, f"{p['id']}.md"), 'w') as fh:
            fh.write(f"путь: {p['path']}\n{text}\n")
    with open(os.path.join(out_dir, 'index.tsv'), 'w') as fh:
        for p in sorted(pages, key=lambda x: x['path']):
            fh.write(f"{p['id']}\t{p['parentId'] or ''}\t{p['version']}\t{p['updated'][:10]}\t{p['path']}\n")
    return pages


def confluence_api_search(cql: str, limit: int = 50, env: dict | None = None) -> list:
    """CQL-поиск (v1 `rest/api/search`): `id`, `title`, `space`, `excerpt`, `url`."""
    found = _confluence_get('wiki/rest/api/search', {'cql': cql, 'limit': limit}, env)
    out = []
    for r in found.get('results', []):
        content = r.get('content', {})
        out.append({'id': content.get('id'), 'title': content.get('title') or r.get('title'),
                    'space': r.get('resultGlobalContainer', {}).get('title', ''),
                    'excerpt': re.sub(r'@@@\w*@@@', '', r.get('excerpt', '')).strip(),
                    'url': r.get('url', '')})
    return out


def confluence_api_storage_text(storage: str) -> str:
    """Storage XHTML Confluence → markdown-подобный текст."""
    soup = BeautifulSoup(storage or '', 'html.parser')
    text = _confluence_node(soup)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _confluence_get(path, params, env):
    return jira_api_request('GET', path, params=params, env=env, product='confluence')


def _confluence_next(page):
    nxt = page.get('_links', {}).get('next')
    if not nxt:
        return None, None
    parsed = urllib.parse.urlparse(nxt)
    return parsed.path.lstrip('/'), dict(urllib.parse.parse_qsl(parsed.query))


def _confluence_node(node) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r'\s+', ' ', str(node))
    if not isinstance(node, Tag):
        return ''
    name = node.name
    if name == 'ac:structured-macro':
        macro = node.get('ac:name', '')
        body = node.find('ac:plain-text-body')
        if body is not None:
            lang = node.find('ac:parameter', {'ac:name': 'language'})
            return f"\n```{lang.get_text() if lang else ''}\n{body.get_text()}\n```\n"
        rich = node.find('ac:rich-text-body')
        inner = _confluence_children(rich) if rich else ''
        if macro in ('info', 'note', 'warning', 'tip', 'panel', 'expand'):
            return f'\n[{macro}] {inner.strip()}\n'
        return inner or (f'[{macro}]' if macro not in ('toc', 'children', 'anchor') else '')
    if name == 'ac:link':
        page = node.find('ri:page')
        label = node.find(['ac:plain-text-link-body', 'ac:link-body'])
        title = page.get('ri:content-title', '') if page else ''
        return f"[{(label.get_text() if label else '') or title}]"
    if name == 'ac:image':
        att = node.find('ri:attachment')
        return f"[картинка: {att.get('ri:filename', '') if att else ''}]"
    if name in ('ri:attachment',):
        return f"[вложение: {node.get('ri:filename', '')}]"
    if name in ('ac:parameter', 'ac:placeholder'):
        return ''
    if name == 'br':
        return '\n'
    if name == 'a':
        label, href = node.get_text().strip(), node.get('href', '')
        return f'{label} ({href})' if href and href != label else (label or href)
    if name == 'code':
        return f'`{node.get_text()}`'
    if name == 'pre':
        return f'\n```\n{node.get_text()}\n```\n'
    if re.fullmatch(r'h[1-6]', name):
        return '\n' + '#' * (int(name[1]) + 1) + ' ' + _confluence_children(node).strip() + '\n\n'
    if name == 'li':
        return '- ' + _confluence_children(node).strip().replace('\n', '\n  ') + '\n'
    if name in ('ul', 'ol'):
        return '\n' + _confluence_children(node) + '\n'
    if name == 'tr':
        cells = [_confluence_children(c).strip().replace('\n', ' ').replace('|', '\\|')
                 for c in node.find_all(['td', 'th'], recursive=False)]
        return '| ' + ' | '.join(cells) + ' |\n'
    if name == 'table':
        return '\n' + _confluence_children(node) + '\n'
    if name in ('p', 'div', 'blockquote'):
        return _confluence_children(node).strip() + '\n\n'
    return _confluence_children(node)


def _confluence_children(node) -> str:
    return ''.join(_confluence_node(c) for c in node.children)


def _confluence_main():
    parser = argparse.ArgumentParser(description='Confluence Cloud: страница, дерево, выгрузка, поиск.')
    parser.add_argument('--root', default='.', help='каталог проекта с .env.local')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_page = sub.add_parser('page', help='страница текстом')
    p_page.add_argument('page', help='id или ссылка')
    p_page.add_argument('--json', action='store_true')
    p_tree = sub.add_parser('tree', help='дерево пространства')
    p_tree.add_argument('space')
    p_export = sub.add_parser('export', help='всё пространство в каталог')
    p_export.add_argument('space')
    p_export.add_argument('out_dir')
    p_search = sub.add_parser('search', help='CQL-поиск')
    p_search.add_argument('cql')
    p_search.add_argument('--limit', type=int, default=50)
    args = parser.parse_args()

    env = jira_api_env(args.root)
    try:
        if args.cmd == 'page':
            page = confluence_api_page(args.page, env)
            print(json.dumps(page, ensure_ascii=False, indent=2) if args.json
                  else confluence_api_page_text(page, env['url']))
        elif args.cmd == 'tree':
            print(confluence_api_tree(confluence_api_pages(args.space, env)))
        elif args.cmd == 'export':
            pages = confluence_api_export(args.space, args.out_dir, env)
            print(f'{len(pages)} страниц → {args.out_dir} (index.tsv)')
        elif args.cmd == 'search':
            for r in confluence_api_search(args.cql, args.limit, env):
                print(f"{r['id']}\t{r['space']}\t{r['title']}\t{r['excerpt'][:120]}")
    except RuntimeError as err:
        sys.exit(f'confluence: {err}')


if __name__ == '__main__':
    _confluence_main()

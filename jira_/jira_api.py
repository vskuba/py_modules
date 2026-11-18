"""
Jira Cloud REST v3: задача по ключу, поиск по JQL, комментарий — из терминала.

Родился, чтобы агент читал задачи, номера которых ему дают словами, и отвечал
в них комментарием, не открывая браузер.

## Доступ (проверено на protonixltd.atlassian.net, 2026-11)

Ключи — в `.env.local` проекта (`JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`),
читаются `config_env`, в `os.environ` не подмешиваются; переменные окружения
процесса перебивают файл. Авторизация — basic `email:token`.

* **Токен бывает двух видов, и ошибка у них разная.** Классический (без
  scopes) работает на `JIRA_URL`. Scoped-токен на `JIRA_URL` отвечает
  `401 Client must be authenticated` — будто неверный пароль, — а работает
  только через шлюз `https://api.atlassian.com/ex/jira/{cloudId}`. Там же
  токен без Jira-scopes даёт `401 Unauthorized; scope does not match`: почта
  и токен верны, не хватает областей (`read:jira-work`, `write:jira-work`,
  `read:jira-user`). `jira_api_request` пробует прямой адрес, а на 401 —
  шлюз; `cloudId` берётся без авторизации из `{JIRA_URL}/_edge/tenant_info`.
* **Нет задачи и нет прав — один и тот же 404** («Issue does not exist or you
  do not have permission to see it»). Прежде чем решить «нет прав», проверь
  ключ: в git-ветках Cashier задачи зовутся `TIER-1635`, а в Jira это
  `TIER3-1635`. `jira_api_key` переводит по `JIRA_KEY_ALIASES` из env
  (`TIER:TIER3,ABC:ABC2` — знание проекта, в слое его нет).
* **Поиск — `POST /rest/api/3/search/jql`**: старый `/rest/api/3/search`
  Atlassian выключил (410). Пагинация — `nextPageToken`, не `startAt`; поля
  надо перечислить явно, иначе вернутся только id.
* **Описание и комментарии — ADF** (JSON-дерево), не текст. Читаем через
  `jira_api_adf_text`, пишем через `jira_api_text_adf` (абзацы, списки `- `,
  блоки ``` ```).

## Что здесь есть

* `jira_api_env` — url/email/token и псевдонимы ключей из `.env.local`.
* `jira_api_key` — ключ из «TIER-1635», ветки `fix/TIER-1635-x`, ссылки.
* `jira_api_request` — запрос с выбором прямого адреса или шлюза.
* `jira_api_issue` / `jira_api_issue_text` — задача сырым JSON / читаемым текстом.
* `jira_api_search` — JQL, свежие сверху.
* `jira_api_comment` — добавить комментарий (⚠ публикация от имени владельца токена).
* `jira_api_attach` — приложить файлы к задаче (⚠ тоже публикация).
* `jira_api_comment_update` / `jira_api_attachments_media` — правка комментария с картинками задачи.
* `jira_api_media_id` — media-uuid вложения для картинки в комментарии (только из 303 `Location`).
* `jira_api_adf_text` / `jira_api_text_adf` — ADF ↔ текст.

CLI:

    python -m jira_.jira_api issue TIER-1635                 # описание + комментарии
    python -m jira_.jira_api issue TIER3-1637 --json         # сырой JSON
    python -m jira_.jira_api search 'assignee = currentUser() AND statusCategory != Done'
    python -m jira_.jira_api comment TIER3-1637 'Текст'      # или '-' — текст из stdin
    python -m jira_.jira_api comment TIER3-1637 - --attach a.png b.png   # `!a.png!` в тексте — картинка
    python -m jira_.jira_api comment TIER3-1637 - --edit 256359 --attach c.png  # правка, старые картинки — по имени
    python -m jira_.jira_api myself                          # проверка доступа

`--root` — каталог проекта с `.env.local` (по умолчанию текущий).
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from config.config import config_env

ISSUE_FIELDS = ['summary', 'status', 'assignee', 'reporter', 'priority', 'issuetype',
                'created', 'updated', 'description', 'comment', 'labels', 'parent']
_GATEWAY_CACHE: dict = {}


def jira_api_env(root: str = '.') -> dict:
    """Доступ к Jira: `url`, `email`, `token`, `aliases` — окружение процесса, иначе `.env.local`.

    `aliases` — из `JIRA_KEY_ALIASES` (`TIER:TIER3,…`), пустой словарь, если не задано.

    Raises:
        RuntimeError: каких-то ключей нет.
    """
    file_env = config_env(root)
    env = {k: os.environ.get(f'JIRA_{k.upper()}') or file_env.get(f'JIRA_{k.upper()}', '')
           for k in ('url', 'email', 'api_token')}
    missing = [f'JIRA_{k.upper()}' for k, v in env.items() if not v]
    if missing:
        raise RuntimeError(f'нет {", ".join(missing)} ни в окружении, ни в {root}/.env.local')
    raw_aliases = os.environ.get('JIRA_KEY_ALIASES') or file_env.get('JIRA_KEY_ALIASES', '')
    aliases = dict(pair.split(':', 1) for pair in raw_aliases.replace(' ', '').split(',') if ':' in pair)
    return {'url': env['url'].rstrip('/'), 'email': env['email'], 'token': env['api_token'],
            'aliases': aliases}


def jira_api_key(text: str, aliases: dict | None = None) -> str:
    """Ключ задачи из строки: `TIER-1635`, `fix/TIER-1635-foo`, ссылки `/browse/…`.

    Префиксы из `aliases` переводятся (`{'TIER': 'TIER3'}`: `TIER-1635` → `TIER3-1635`).

    Raises:
        ValueError: ключа в строке нет.
    """
    match = re.search(r'\b([A-Z][A-Z0-9]+)-(\d+)\b', text.upper())
    if not match:
        raise ValueError(f'в «{text}» нет ключа задачи вида ABC-123')
    project = (aliases or {}).get(match.group(1), match.group(1))
    return f'{project}-{match.group(2)}'


def jira_api_request(method: str, path: str, body: dict | None = None,
                     params: dict | None = None, env: dict | None = None,
                     timeout: float = 20, product: str = 'jira',
                     raw: bytes | None = None, headers: dict | None = None):
    """Запрос к REST Jira; вернёт разобранный JSON (или None на пустой ответ).

    Сначала прямой `JIRA_URL`, на 401 — шлюз `api.atlassian.com` (scoped-токен);
    удачный вариант запоминается на процесс.

    Args:
        path: путь от корня, напр. `rest/api/3/myself` (Confluence — `wiki/api/v2/...`).
        product: `jira` или `confluence` — у шлюза разные префиксы
            (`/ex/jira/{cloudId}`, `/ex/confluence/{cloudId}`); сайт и токен общие.
        raw, headers: готовое тело (напр. multipart) и свои заголовки вместо JSON `body`.

    Raises:
        RuntimeError: HTTP-ошибка — код и начало ответа в сообщении.
    """
    env = env or jira_api_env()
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, '')})
    tail = path.lstrip('/') + (f'?{query}' if query else '')
    cache_key = (env['url'], product)
    bases = [_GATEWAY_CACHE[cache_key]] if cache_key in _GATEWAY_CACHE else [env['url'], None]
    error = None
    for base in bases:
        base = base or _jira_gateway(env['url'], product)
        try:
            result = _jira_http(method, f'{base}/{tail}', body, env, timeout, raw, headers)
            _GATEWAY_CACHE[cache_key] = base
            return result
        except urllib.error.HTTPError as err:
            error = RuntimeError(f'{err.code} {method} {base}/{tail}: '
                                 f'{err.read().decode(errors="replace")[:300]}')
            if err.code != 401:
                break
    raise error


def jira_api_issue(key: str, env: dict | None = None) -> dict:
    """Задача сырым JSON с полями `ISSUE_FIELDS` (ключ пропускается через `jira_api_key`)."""
    env = env or jira_api_env()
    return jira_api_request('GET', f"rest/api/3/issue/{jira_api_key(key, env['aliases'])}",
                            params={'fields': ','.join(ISSUE_FIELDS)}, env=env)


def jira_api_issue_text(issue: dict, base_url: str = '') -> str:
    """Задача читаемым текстом: шапка, описание, комментарии по времени."""
    f = issue.get('fields', {})
    name = lambda user: (user or {}).get('displayName', '—')
    lines = [
        f"{issue['key']}  [{(f.get('issuetype') or {}).get('name', '')}]  {f.get('summary', '')}",
        f"статус: {(f.get('status') or {}).get('name', '')} · исполнитель: {name(f.get('assignee'))}"
        f" · автор: {name(f.get('reporter'))} · приоритет: {(f.get('priority') or {}).get('name', '')}",
        f"создана {f.get('created', '')[:16]} · обновлена {f.get('updated', '')[:16]}",
    ]
    if f.get('parent'):
        lines.append(f"родитель: {f['parent']['key']} {f['parent']['fields'].get('summary', '')}")
    if f.get('labels'):
        lines.append('метки: ' + ', '.join(f['labels']))
    if base_url:
        lines.append(f"{base_url}/browse/{issue['key']}")
    lines += ['', '## Описание', jira_api_adf_text(f.get('description')) or '—']
    comments = (f.get('comment') or {}).get('comments', [])
    lines += ['', f'## Комментарии ({len(comments)})']
    for c in comments:
        lines += ['', f"— {name(c.get('author'))}, {c.get('created', '')[:16]}:",
                  jira_api_adf_text(c.get('body'))]
    return '\n'.join(lines)


def jira_api_search(jql: str, fields: list | None = None, limit: int = 50,
                    env: dict | None = None) -> list:
    """Задачи по JQL (до `limit`), листает `nextPageToken` сам."""
    issues, token = [], None
    while len(issues) < limit:
        body = {'jql': jql, 'maxResults': min(100, limit - len(issues)),
                'fields': fields or ['summary', 'status', 'assignee', 'updated']}
        if token:
            body['nextPageToken'] = token
        page = jira_api_request('POST', 'rest/api/3/search/jql', body=body, env=env)
        issues += page.get('issues', [])
        token = page.get('nextPageToken')
        if not token or page.get('isLast'):
            break
    return issues


def jira_api_comment(key: str, text: str, env: dict | None = None,
                     media: dict | None = None) -> dict:
    """Добавить комментарий к задаче; вернёт созданный комментарий (`id`, `self`).

    `media` — {имя файла: media-uuid} для строк `!имя!` в тексте (см. `jira_api_text_adf`);
    собирается `jira_api_attach` + `jira_api_media_id`, CLI делает это сам (`--attach`).

    ⚠ Публикация от имени владельца токена — звать только по прямой просьбе.
    """
    env = env or jira_api_env()
    return jira_api_request('POST', f"rest/api/3/issue/{jira_api_key(key, env['aliases'])}/comment",
                            body={'body': jira_api_text_adf(text, media)}, env=env)


def jira_api_comment_update(key: str, comment_id: str, text: str, env: dict | None = None,
                            media: dict | None = None) -> dict:
    """Заменить текст комментария целиком (`PUT …/comment/{id}`); правило `!имя!` то же.

    ⚠ Правка опубликованного — тоже публикация. Картинки прежней версии не
    подтягиваются сами: их имена должны быть в `media` (`jira_api_attachments_media`).
    """
    env = env or jira_api_env()
    return jira_api_request('PUT', f"rest/api/3/issue/{jira_api_key(key, env['aliases'])}/comment/{comment_id}",
                            body={'body': jira_api_text_adf(text, media)}, env=env)


def jira_api_attachments_media(key: str, env: dict | None = None) -> dict:
    """{имя файла: media-uuid} для всех вложений задачи — чтобы снова вставить их в текст."""
    env = env or jira_api_env()
    issue = jira_api_request('GET', f"rest/api/3/issue/{jira_api_key(key, env['aliases'])}",
                             params={'fields': 'attachment'}, env=env)
    return {a['filename']: jira_api_media_id(a['id'], env) for a in issue['fields']['attachment']}


def jira_api_media_id(attachment_id: str, env: dict | None = None) -> str:
    """UUID файла в Media Services по id вложения — его ждёт ADF-узел `media`.

    В метаданных вложения (`rest/api/3/attachment/{id}`) этого UUID нет. Он есть
    только в `Location` ответа 303 на `rest/api/3/attachment/content/{id}`
    (`…api.media.atlassian.com/file/<uuid>/binary…`), поэтому редирект не выполняем.
    С `?redirect=false` Jira отдаёт сам файл — UUID не узнать.
    """
    env = env or jira_api_env()
    jira_api_request('GET', 'rest/api/3/myself', env=env)  # выбрать прямой адрес или шлюз
    base = _GATEWAY_CACHE[(env['url'], 'jira')]
    auth = base64.b64encode(f"{env['email']}:{env['token']}".encode()).decode()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    req = urllib.request.Request(f'{base}/rest/api/3/attachment/content/{attachment_id}',
                                 headers={'Authorization': f'Basic {auth}'})
    try:
        urllib.request.build_opener(_NoRedirect).open(req, timeout=20)
    except urllib.error.HTTPError as err:
        match = re.search(r'/file/([0-9a-f-]{36})/', err.headers.get('Location') or '')
        if err.code in (301, 302, 303, 307) and match:
            return match.group(1)
        raise RuntimeError(f'{err.code} attachment/content/{attachment_id}: нет media-uuid в Location')
    raise RuntimeError(f'attachment/content/{attachment_id}: ждали 303, пришёл файл')


def jira_api_attach(key: str, paths: list, env: dict | None = None) -> list:
    """Приложить файлы к задаче; вернёт список вложений (`id`, `filename`, `content`).

    ⚠ Публикация: вложение видно всем, кто видит задачу, — звать только по просьбе.
    Multipart на `rest/api/3/issue/{key}/attachments` с `X-Atlassian-Token: no-check`
    (без него Jira отвечает 403 XSRF check failed).
    """
    env = env or jira_api_env()
    boundary = 'jira-api-' + base64.b16encode(os.urandom(8)).decode().lower()
    parts = []
    for path in paths:
        name = os.path.basename(path)
        with open(path, 'rb') as fh:
            payload = fh.read()
        mime = 'image/png' if name.lower().endswith('.png') else 'application/octet-stream'
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                     f'filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + payload + b'\r\n')
    raw = b''.join(parts) + f'--{boundary}--\r\n'.encode()
    return jira_api_request('POST', f"rest/api/3/issue/{jira_api_key(key, env['aliases'])}/attachments",
                            env=env, timeout=60, raw=raw, headers={
                                'Content-Type': f'multipart/form-data; boundary={boundary}',
                                'X-Atlassian-Token': 'no-check'})


def jira_api_adf_text(node) -> str:
    """ADF (описание, комментарий) → простой текст; None → ''."""
    if not node:
        return ''
    if isinstance(node, str):
        return node
    return _jira_adf_walk(node).strip()


def jira_api_text_adf(text: str, media: dict | None = None) -> dict:
    """Текст → ADF-документ: абзацы по пустой строке, `- ` — список, ``` — код.

    Строка `!имя!` (имя из `media`) — картинка (`mediaSingle`); `[~accountid:ID|Имя]`
    внутри строки — упоминание (Jira уведомит человека, как при «@Имя» в браузере).
    """
    media = media or {}
    content, para, items = [], [], []
    lines = text.splitlines()
    i = 0

    def flush():
        if para:
            inline = []
            for n, line in enumerate(para):
                if n:
                    inline.append({'type': 'hardBreak'})
                inline.extend(_jira_inline(line))
            content.append({'type': 'paragraph', 'content': inline})
            para.clear()
        if items:
            content.append({'type': 'bulletList', 'content': [
                {'type': 'listItem', 'content': [
                    {'type': 'paragraph', 'content': [{'type': 'text', 'text': it}]}]}
                for it in items]})
            items.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith('```'):
            flush()
            lang, code = line[3:].strip(), []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                code.append(lines[i])
                i += 1
            block = {'type': 'codeBlock', 'content': [{'type': 'text', 'text': '\n'.join(code) or ' '}]}
            if lang:
                block['attrs'] = {'language': lang}
            content.append(block)
        elif re.fullmatch(r'!(.+)!', line.strip()) and line.strip()[1:-1] in media:
            flush()
            content.append({'type': 'mediaSingle', 'attrs': {'layout': 'align-start'}, 'content': [
                {'type': 'media', 'attrs': {'type': 'file', 'id': media[line.strip()[1:-1]],
                                            'collection': '', 'alt': line.strip()[1:-1]}}]})
        elif re.match(r'^\s*[-*] ', line):
            if para:
                flush()
            items.append(re.sub(r'^\s*[-*] ', '', line))
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            para.append(line)
        i += 1
    flush()
    return {'type': 'doc', 'version': 1, 'content': content}


def _jira_inline(line: str) -> list:
    """Строка → ADF-узлы: текст и упоминания `[~accountid:ID|Имя]`."""
    nodes, pos = [], 0
    for m in re.finditer(r'\[~accountid:([^|\]]+)\|([^\]]+)\]', line):
        if m.start() > pos:
            nodes.append({'type': 'text', 'text': line[pos:m.start()]})
        nodes.append({'type': 'mention', 'attrs': {'id': m.group(1), 'text': '@' + m.group(2)}})
        pos = m.end()
    if pos < len(line):
        nodes.append({'type': 'text', 'text': line[pos:]})
    return nodes


def _jira_http(method, url, body, env, timeout, raw=None, headers=None):
    auth = base64.b64encode(f"{env['email']}:{env['token']}".encode()).decode()
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Basic {auth}', 'Accept': 'application/json',
        **({'Content-Type': 'application/json'} if data and raw is None else {}),
        **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _jira_gateway(url, product='jira'):
    with urllib.request.urlopen(f'{url}/_edge/tenant_info', timeout=10) as resp:
        cloud_id = json.loads(resp.read())['cloudId']
    return f'https://api.atlassian.com/ex/{product}/{cloud_id}'


def _jira_adf_walk(node) -> str:
    kind, attrs = node.get('type'), node.get('attrs') or {}
    if kind == 'text':
        text = node.get('text', '')
        for mark in node.get('marks', []):
            if mark.get('type') == 'link' and mark.get('attrs', {}).get('href') not in (None, text):
                text = f"{text} ({mark['attrs']['href']})"
            elif mark.get('type') == 'code':
                text = f'`{text}`'
        return text
    if kind == 'hardBreak':
        return '\n'
    if kind == 'mention':
        return attrs.get('text', '@?')
    if kind in ('inlineCard', 'blockCard', 'embedCard'):
        return attrs.get('url', '')
    if kind == 'emoji':
        return attrs.get('text') or attrs.get('shortName', '')
    if kind in ('media', 'mediaSingle', 'mediaGroup') and not node.get('content'):
        return '[вложение]'
    inner = ''.join(_jira_adf_walk(c) for c in node.get('content', []))
    if kind == 'mediaSingle' or kind == 'mediaGroup':
        return '[вложение]\n'
    if kind == 'codeBlock':
        return f"```{attrs.get('language', '')}\n{inner}\n```\n\n"
    if kind == 'heading':
        return '#' * attrs.get('level', 2) + ' ' + inner + '\n\n'
    if kind == 'listItem':
        return '- ' + inner.strip().replace('\n', '\n  ') + '\n'
    if kind in ('bulletList', 'orderedList'):
        return inner + '\n'
    if kind == 'tableRow':
        return '| ' + ' | '.join(_jira_adf_walk(c).strip() for c in node.get('content', [])) + ' |\n'
    if kind in ('paragraph', 'blockquote', 'panel', 'table', 'rule'):
        return (inner if kind != 'rule' else '---') + ('\n' if kind == 'table' else '\n\n')
    return inner


def _jira_main():
    parser = argparse.ArgumentParser(description='Jira Cloud: задача, поиск, комментарий.')
    parser.add_argument('--root', default='.', help='каталог проекта с .env.local')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_issue = sub.add_parser('issue', help='задача по ключу')
    p_issue.add_argument('key')
    p_issue.add_argument('--json', action='store_true', help='сырой JSON')
    p_search = sub.add_parser('search', help='задачи по JQL')
    p_search.add_argument('jql')
    p_search.add_argument('--limit', type=int, default=50)
    p_comment = sub.add_parser('comment', help='добавить комментарий (публикация!)')
    p_comment.add_argument('key')
    p_comment.add_argument('text', help="текст или '-' — из stdin")
    p_comment.add_argument('--attach', nargs='+', default=[],
                           help='файлы: приложить к задаче; строка `!имя!` в тексте вставит картинку')
    p_comment.add_argument('--edit', default='', help='id комментария: заменить его текст, а не добавлять новый')
    sub.add_parser('myself', help='проверка доступа')
    args = parser.parse_args()

    env = jira_api_env(args.root)
    try:
        if args.cmd == 'issue':
            issue = jira_api_issue(args.key, env)
            print(json.dumps(issue, ensure_ascii=False, indent=2) if args.json
                  else jira_api_issue_text(issue, env['url']))
        elif args.cmd == 'search':
            for it in jira_api_search(args.jql, limit=args.limit, env=env):
                f = it['fields']
                print(f"{it['key']}\t{(f.get('status') or {}).get('name', '')}\t"
                      f"{(f.get('assignee') or {}).get('displayName', '—')}\t{f.get('summary', '')}")
        elif args.cmd == 'comment':
            text = sys.stdin.read() if args.text == '-' else args.text
            key = jira_api_key(args.key, env['aliases'])
            if args.attach:
                jira_api_attach(key, args.attach, env)
            media = jira_api_attachments_media(key, env) if (args.attach or args.edit) else {}
            made = (jira_api_comment_update(key, args.edit, text, env, media) if args.edit
                    else jira_api_comment(key, text, env, media))
            print(f"комментарий {made['id']}: {env['url']}/browse/{key}"
                  f"?focusedCommentId={made['id']}")
        elif args.cmd == 'myself':
            me = jira_api_request('GET', 'rest/api/3/myself', env=env)
            print(f"{me['displayName']} · {me['emailAddress']} · {me['accountId']}")
    except RuntimeError as err:
        sys.exit(f'jira: {err}')


if __name__ == '__main__':
    _jira_main()

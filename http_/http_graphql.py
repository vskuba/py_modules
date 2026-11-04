"""
GraphQL-запрос без пантомимы с кавычками: тело собирает json.dumps, не шелл.

Однострочный GraphQL руками — вечная лотерея экранирования: двойные кавычки
запроса ломают `curl -d "..."`, heredoc `<<EOF` подставляет переменные и
съедает `\\`, `<<'EOF'` лишает переменных, а кривое тело возвращается невнятным
«Error parsing request body» — и полчаса уходят не на запрос, а на доставку
целого JSON до сервера. Отсюда один вход: `http_graphql_post` — запрос,
переменные и заголовки словами, ответ разобранным dict-ом; ошибки сервера
(`errors`) — значение, а не исключение: зонд схемы (`Cannot query field`) —
нормальный рабочий ответ, его и надо видеть, а не ловить traceback.

    python -m http_.http_graphql https://app.circleci.com/graphql \\
        '{ pipeline(id: "x") { name } }' -H "Authorization: Bearer $(cat ~/.token)"

⚠ Некоторые GraphQL (напр. CircleCI) на запросы отвечают, но интроспекцию
держат закрытой и нужных полей не отдают: это повод уйти в REST, а не
упорствовать в переборе имён — каждая догадка дёшева, но время съедает.
"""
import argparse
import json
import urllib.error
import urllib.request


def http_graphql_post(url: str, query: str, variables: dict | None = None,
                      headers: dict | None = None, timeout: float = 15) -> dict:
    """POST GraphQL; вернёт разобранный JSON ответа.

    Args:
        url: эндпоинт GraphQL.
        query: текст запроса, как есть (кавычки не экранировать — не шелл).
        variables: значения для `$переменных`.
        headers: заголовки авторизации и прочее.
        timeout: сек.

    Returns:
        `{'data': ..., 'errors': [...], 'raw': текст_без_разбора}`; ключ
        `errors` пустым списком — сервер ответил чисто. HTTP-ошибки — ValueError
        с текстом ответа (GraphQL-ошибки ошибкой HTTP не считаются).
    """
    body = {'query': query}
    if variables:
        body['variables'] = variables
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode(errors='replace')
    except urllib.error.HTTPError as err:
        text = err.read().decode(errors='replace')
        try:
            got = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError(f'HTTP {err.code}: {text[:200]}')
        if 'errors' not in got:
            raise ValueError(f'HTTP {err.code}: {text[:200]}')
        return {'data': got.get('data'), 'errors': got['errors'], 'raw': text}
    except urllib.error.URLError as err:
        raise ValueError(f'GraphQL-эндпоинт недоступен: {err}')
    try:
        got = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(f'ответ не JSON: {text[:200]}')
    return {'data': got.get('data'), 'errors': got.get('errors', []), 'raw': text}


def _cli(argv=None):
    ap = argparse.ArgumentParser(description='POST GraphQL с разбором ответа.')
    ap.add_argument('url')
    ap.add_argument('query')
    ap.add_argument('-H', '--header', action='append', default=[],
                    help='Заголовок K:V, повторно')
    ap.add_argument('--vars', default='', help='JSON переменных')
    a = ap.parse_args(argv)
    headers = dict(h.split(':', 1) for h in a.header)
    got = http_graphql_post(a.url, a.query,
                            variables=json.loads(a.vars) if a.vars else None,
                            headers=headers)
    if got['errors']:
        print('ОШИБКИ:', json.dumps(got['errors'], ensure_ascii=False)[:2000])
        raise SystemExit(4)
    print(json.dumps(got['data'], ensure_ascii=False, indent=1)[:4000])


if __name__ == '__main__':
    _cli()

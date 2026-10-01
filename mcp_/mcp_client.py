"""MCP streamable-HTTP клиент: handshake, tools/list, tools/call — одним вызовом.

Треть агентных API сегодня отдаёт свои руки через MCP (JSON-RPC поверх HTTP с
`Mcp-Session-Id` и SSE-ответами), и каждый раз собирался один и тот же бойлер-
плейт: initialize → notifications/initialized → tools/call, разбор `data:`-
строк. Плюс главная грабля живых серверов: **MCP не обязан пробрасывать ключ
в апстрим** — ключевой вызов обязан заканчиваться честным REST, если MCP его
не донёс.
"""

_MCP_CLIENT_PROTOCOL = '2025-06-18'


def mcp_client_tools(url: str, headers: dict | None = None) -> dict:
    """Список инструментов MCP-сервера: {'tools': [{name, required, properties}]}.

    Args:
        url: адрес точки MCP (обычно https://…/mcp).
        headers: заголовки запроса (например ключ) — что сервер пропустит в
            апстрим, решено на caller'е, не здесь.
    """
    return {'tools': _rpc(url, 'tools/list', {}, headers)}


def mcp_client_call(url: str, tool: str, arguments: dict,
                    headers: dict | None = None) -> dict:
    """Вызвать инструмент MCP и вернуть его результат (или {'error': …}).

    ⚠ Часть серверов не пробрасывает авторизационные заголовки в апстрим:
    «Missing API key» из результата — не ошибка ключа, а факт про сервер;
    тогда зови тот же инструмент честным REST.
    """
    r = _rpc(url, 'tools/call', {'name': tool, 'arguments': arguments}, headers)
    return r


# ─── приватное ─────────────────────────────────────────────────────────────

def _rpc(url: str, method: str, params: dict, headers: dict | None):
    import json
    import urllib.request
    h = {'content-type': 'application/json', 'accept': 'application/json, text/event-stream'}
    h.update(headers or {})
    init = {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
        'protocolVersion': _MCP_CLIENT_PROTOCOL, 'clientInfo': {'name': 'py_modules', 'version': '1'},
        'capabilities': {}}}
    req = urllib.request.Request(url, data=json.dumps(init).encode(),
                                headers=h, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        sid = resp.headers.get('mcp-session-id')
        resp.read()
    hh = dict(h)
    if sid:
        hh['mcp-session-id'] = sid
    body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    if sid:
        notify = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps(notify).encode(), headers=hh, method='POST'), timeout=20).read()
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=hh, method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        j = json.loads(_body(resp.read().decode()).strip() or '{}')
    if method == 'tools/list':
        return j.get('result', {}).get('tools', [])
    res = j.get('result', {})
    if res.get('isError'):
        return {'error': str(res)[:400]}
    txt = ''.join(c.get('text', '') for c in res.get('content', []) if isinstance(c, dict))
    try:
        return json.loads(txt)
    except (ValueError, TypeError):
        return {'text': txt}


def _body(raw: str) -> str:
    out = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith('data:')]
    return '\n'.join(out) if out else raw

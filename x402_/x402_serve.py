"""x402: платный 402-челлендж на FastAPI-маршруты — продавать данные агентам.

Закрываешь уже собранный app платным слоем: без оплаты — 402 с заголовком
`payment-required` и телем-требованием, с оплатой — обработчик. Посредник
(facilitator) — публичный x402.org, сеть — testnet Base Sepolia.

⚠ network в routes — каноническим id (`eip155:84532`), а не «base»: публичный
  facilitator mainnet не обслуживает и молча молчит про чужие сети;
⚠ price — строка денег («$0.01»), не float: float разбирается как доллары и
  центы по своей формуле, строка точна;
⚠ ключ routes — «GET /путь» одной строкой (метод + пробел + путь), путь
  сверяется по raw_path маршрутизатора.
"""

from __future__ import annotations


def x402_serve_wrap(app, routes: dict) -> object:
    """Защитить маршруты FastAPI-приложения платным 402-челленджем (x402 v2).

    routes — {«МЕТОД /путь»: {'accepts': {'scheme': 'exact', 'payTo': '0x…',
    'price': '$0.01', 'network': 'eip155:84532'}}}; возвращает тот же app.
    """
    from x402.server import x402ResourceServer
    from x402.http import HTTPFacilitatorClient
    from x402.http.middleware.fastapi import payment_middleware
    from x402.mechanisms.evm.exact.register import register_exact_evm_server
    from starlette.middleware.base import BaseHTTPMiddleware

    server = x402ResourceServer(HTTPFacilitatorClient())
    register_exact_evm_server(server)
    mw = payment_middleware(routes, server)

    class _X402Paywall(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await mw(request, call_next)

    app.add_middleware(_X402Paywall)
    return app


def x402_serve_demo(port: int = 8137) -> None:
    """Поднять демо-сервис с одним платным маршрутом — проверить трек живьём.

    CLI: `python -m x402_.x402_serve demo [--port N]`; без оплаты GET вернёт
    402 с `payment-required` — это и есть проверка, что стек живой.
    """
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()

    @app.get('/npm/downloads')
    async def downloads(pkg: str = 'react'):
        import urllib.request, json as _json
        with urllib.request.urlopen(
            f'https://api.npmjs.org/downloads/point/last-week/{pkg}', timeout=15
        ) as r:
            return _json.load(r)

    app = x402_serve_wrap(app, {
        'GET /npm/downloads': {'accepts': {
            'scheme': 'exact',
            'payTo': '0x90B095ECF809F56ebF16327224c0276948cfb1bc',
            'price': '$0.01', 'network': 'eip155:84532'}},
    })
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(prog='x402_serve')
    p.add_argument('cmd', choices=['demo'])
    p.add_argument('--port', type=int, default=8137)
    a = p.parse_args()
    {'demo': x402_serve_demo}[a.cmd](a.port)

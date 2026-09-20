# x402: платный 402-челлендж на FastAPI-маршруты (`x402_serve`)

Зачем: продаёшь данные агентам — продаёшь HTTP, а не обещание; unpaid GET
обязан отвечать 402 с требованием оплаты, paid — телом маршрута.

## Устройство

- `x402_serve_wrap(app, routes)` — лениво поднимает `x402` (server + facilitator
  + exact/EVM-схема), вешает middleware на app; routes — «МЕТОД /путь» → accepts.
- `x402_serve_demo(port)` — живой тест трека: один платный маршрут, unpaid → 402.

## Грабли

- `network` — канонический id (`eip155:84532`), не «base»: публичный facilitator
  x402.org — testnet, mainnet не обслуживает; чужая сеть → «no supported payment
  kinds loaded».
- `price` — строка денег («$0.01»): float разбирается иначе, строка точна.
- `accepts` — camelCase (`payTo`), не snake_case: pydantic-алиасы чужие.
- schemes: без `register_exact_evm_server(server)` middleware поднимется, но
  умрёт на первом же защищённом запросе («No scheme implementation registered»).

## Проверка

Одного импорта мало (ленивые импорты не проверяют `x402`): `python -m
x402_.x402_serve demo` + curl без оплаты → 402 с `payment-required`.

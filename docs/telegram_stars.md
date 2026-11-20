# Telegram Stars: оплата в боте звёздами

> Звёзды (`XTR`) — валюта внутри Telegram: бот берёт их за цифровые товары и
> услуги без платёжного провайдера, KYC и эквайринга. Модуль
> `telegram_/telegram_stars.py` — это весь обмен Bot API вокруг оплаты: счёт,
> подтверждение, зачисление, возврат и сверка. От библиотеки бота он не зависит:
> на входе токен и апдейт как `dict`, на выходе `{'ok', …, 'error'}`.

## Ход одной оплаты

```python
from telegram_.telegram_stars import (telegram_stars_checkout, telegram_stars_checkout_answer,
                                      telegram_stars_invoice_send, telegram_stars_payment,
                                      telegram_stars_refund)

# 1. счёт: payload — ваш ключ заказа (1–128 байт), цена — целые звёзды
await telegram_stars_invoice_send(token, chat_id, 'Апскейл ×4', 'Чёткое фото из мелкого',
                                  payload=f'order-{order_id}', amount=20)

# 2. «Оплатить» нажато — ответить за 10 секунд
if (query := telegram_stars_checkout(update)):
    order = await order_get(query['payload'])           # быстрая проверка, не прогон
    await telegram_stars_checkout_answer(token, query['query_id'],
                                         ok=order is not None and order.price == query['amount'],
                                         error='Заказ устарел, создайте новый')

# 3. деньги списаны — зачислить ровно один раз
if (paid := telegram_stars_payment(update)):
    await payment_save(paid['charge_id'], paid['user_id'], paid['amount'], paid['payload'])
    # INSERT с UNIQUE(charge_id): повторный апдейт — no-op, а не двойной баланс

# 4. услуга не вышла — вернуть
await telegram_stars_refund(token, paid['user_id'], paid['charge_id'])
```

Ссылка на счёт вместо сообщения (кнопка, Mini App `openInvoice`, пост в канале)
— `telegram_stars_invoice_link`. Сверка учёта: `telegram_stars_balance` —
звёзды на счету бота, `telegram_stars_transactions` — журнал поступлений (`in`)
и возвратов (`out`); `id` входящей записи равен `charge_id` оплаты.

```bash
# токен — из TELEGRAM_BOT_TOKEN (окружение или .env проекта)
PYTHONPATH=py_modules python -m telegram_.telegram_stars balance
PYTHONPATH=py_modules python -m telegram_.telegram_stars transactions --limit 20
PYTHONPATH=py_modules python -m telegram_.telegram_stars link 'Апскейл' 20
```

## Грабли

1. **«Да» на `pre_checkout_query` — ещё не деньги.** Списание может не пройти
   после него. Единственное подтверждение — `successful_payment`; выдача
   услуги по `pre_checkout` — выдача даром.
2. **Апдейт об оплате приходит повторно**, если бот не подтвердил его вовремя
   (рестарт посреди обработки). Зачислять с уникальным ключом на `charge_id`.
3. **На ответ `pre_checkout_query` — 10 секунд.** Проверка там — только быстрая
   (заказ есть, цена совпала); прогон, генерация, обращение к ферме — после
   `successful_payment`.
4. **`provider_token` для звёзд — пустой.** Модуль его не передаёт вовсе;
   непустой Telegram отклоняет. Цена — ровно одна строка `prices`.
5. **Ссылка на счёт многоразовая**: по ней платит любой и сколько угодно раз.
   Заказ узнаётся по `payload` и `user_id` из оплаты, а не по ссылке.
6. **Повторный возврат** отвечает `CHARGE_ALREADY_REFUNDED` (`ok=False`), хотя
   деньги уже у пользователя: отмечайте возврат у себя по первому успеху.
7. **Токен — в адресе запроса.** Текст ошибки собирается из `description` Bot
   API или типа исключения, не из `str(exc)` httpx, где адрес целиком.
8. **Отказ без текста** (`ok=False` без `error`) Telegram не принимает — модуль
   отвечает `ok=False` до похода в сеть.

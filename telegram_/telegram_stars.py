"""Оплата в боте звёздами Telegram (Stars, валюта `XTR`): счёт, подтверждение, зачисление, возврат.

Stars — цифровая валюта внутри Telegram: пользователь покупает её в приложении,
бот получает её за цифровые товары и услуги без платёжного провайдера, KYC и
карточного эквайринга. Весь обмен — четыре вызова Bot API и два вида апдейта,
одинаковые у любого бота. Здесь они собраны без привязки к библиотеке бота
(aiogram, python-telegram-bot, свой long-polling): на входе токен и апдейт как
`dict`, на выходе `{'ok', …, 'error'}`.

## Ход одной оплаты

1. `telegram_stars_invoice_send` (счёт сообщением в чат) или
   `telegram_stars_invoice_link` (ссылка на счёт — для кнопки, Mini App, канала);
2. пользователь жмёт «Оплатить» — приходит апдейт `pre_checkout_query`:
   `telegram_stars_checkout` разбирает его, `telegram_stars_checkout_answer`
   отвечает «да» или «нет со словами» — **за 10 секунд**;
3. деньги списаны — приходит сообщение с `successful_payment`:
   `telegram_stars_payment` разбирает его; зачислять — только отсюда;
4. услуга не вышла — `telegram_stars_refund` возвращает звёзды.

Сверка: `telegram_stars_balance` — сколько звёзд у бота, `telegram_stars_transactions`
— журнал поступлений и возвратов.

⚠⚠ **Подтверждение оплаты — только `successful_payment`.** Ответ «да» на
`pre_checkout_query` ещё не деньги: списание может не пройти, и пользователь
останется без звёзд и без услуги. Зачисление по `pre_checkout` — выдача товара
даром.

⚠⚠ **Зачислять идемпотентно по `charge_id`.** Telegram доставляет апдейт
повторно, если бот не подтвердил его вовремя (рестарт, долгий обработчик), — тот
же платёж придёт дважды. Уникальный ключ на `charge_id` в таблице платежей
превращает второй приход в no-op, а не в двойной баланс.

⚠ Токен бота входит в адрес запроса (`/bot<token>/method`). Ни адрес, ни
исключение httpx с адресом внутри в журнал и в ответ не уходят: текст ошибки
собирается здесь без него.
"""

import argparse
import asyncio

import httpx

from config.config import config_get
from json_.json_out import json_out

# Валюта звёзд в Bot API. ⚠ Другой код (`USD`, `RUB`) требует платёжного
# провайдера и `provider_token` — это уже не Stars.
TELEGRAM_STARS_CURRENCY = 'XTR'

TELEGRAM_STARS_API = 'https://api.telegram.org'

# Сколько секунд у бота на ответ `pre_checkout_query`. Не настройка — правило
# Telegram: не ответил — оплата у пользователя срывается сама.
TELEGRAM_STARS_CHECKOUT_DEADLINE = 10

# Предел `payload` счёта в байтах UTF-8 (Bot API: 1–128).
TELEGRAM_STARS_PAYLOAD_MAX = 128

# Имя переменной с токеном — только для CLI. Функции берут токен доводом:
# как он называется в проекте, слой не знает.
TELEGRAM_STARS_TOKEN_ENV = 'TELEGRAM_BOT_TOKEN'


async def telegram_stars_invoice_send(token: str, chat_id: int, title: str,
                                      description: str, payload: str, amount: int,
                                      *, label: str = '', photo_url: str = '',
                                      timeout: float = 20.0) -> dict:
    """Выставить счёт в звёздах сообщением в чат — кнопка «Оплатить» под ним.

    Args:
        token: токен бота от @BotFather.
        chat_id: куда прислать счёт (личный чат пользователя).
        title: название товара, 1–32 знака.
        description: описание, 1–255 знаков.
        payload: ваш идентификатор заказа, 1–128 байт; вернётся в
            `pre_checkout_query` и `successful_payment` — по нему узнают заказ.
        amount: цена в звёздах, целое ≥ 1.
        label: подпись строки цены; пусто — `title`.
        photo_url: картинка товара в счёте.

    Returns:
        `{'ok': bool, 'message_id': int, 'error': str}`.

    ⚠ `payload` пользователь не видит, но и секретом его не делайте: это ключ
    заказа, а не подпись. Сверяйте заказ по нему у себя в базе.
    """
    params = _invoice_params(title, description, payload, amount, label, photo_url)
    if 'error' in params:
        return {'ok': False, 'message_id': 0, 'error': params['error']}
    params['chat_id'] = chat_id

    answer = await _call(token, 'sendInvoice', params, timeout)
    message = answer['result'] if isinstance(answer['result'], dict) else {}

    return {'ok': answer['ok'], 'message_id': int(message.get('message_id') or 0),
            'error': answer['error']}


async def telegram_stars_invoice_link(token: str, title: str, description: str,
                                      payload: str, amount: int, *, label: str = '',
                                      photo_url: str = '', timeout: float = 20.0) -> dict:
    """Ссылка на счёт в звёздах — для кнопки, Mini App (`openInvoice`) или поста.

    Args — как у `telegram_stars_invoice_send`, без `chat_id`: ссылку открывает
    любой, у кого она есть.

    Returns:
        `{'ok': bool, 'link': 'https://t.me/$…', 'error': str}`.

    ⚠ Ссылка многоразовая: по ней может заплатить любой, и не один раз. Заказ
    узнаётся по `payload` + `user_id` из `successful_payment`, а не по самой ссылке.
    """
    params = _invoice_params(title, description, payload, amount, label, photo_url)
    if 'error' in params:
        return {'ok': False, 'link': '', 'error': params['error']}

    answer = await _call(token, 'createInvoiceLink', params, timeout)

    return {'ok': answer['ok'], 'link': str(answer['result'] or '') if answer['ok'] else '',
            'error': answer['error']}


def telegram_stars_checkout(update: dict) -> dict | None:
    """Разобрать апдейт «пользователь жмёт Оплатить» (`pre_checkout_query`); не он — `None`.

    Returns:
        `{'query_id', 'user_id', 'amount', 'currency', 'payload'}` — то, что нужно,
        чтобы решить, принимать ли оплату, и ответить `telegram_stars_checkout_answer`.
    """
    query = (update or {}).get('pre_checkout_query')
    if not isinstance(query, dict):
        return None

    return {'query_id': str(query.get('id') or ''),
            'user_id': int((query.get('from') or {}).get('id') or 0),
            'amount': int(query.get('total_amount') or 0),
            'currency': str(query.get('currency') or ''),
            'payload': str(query.get('invoice_payload') or '')}


async def telegram_stars_checkout_answer(token: str, query_id: str, ok: bool = True,
                                         error: str = '', *, timeout: float = 5.0) -> dict:
    """Ответить на `pre_checkout_query`: принять оплату или отказать со словами.

    Args:
        query_id: `query_id` из `telegram_stars_checkout`.
        ok: принять оплату.
        error: при отказе — текст, который увидит пользователь («товар закончился»).

    Returns:
        `{'ok': bool, 'error': str}`.

    ⚠⚠ На ответ **10 секунд** (`TELEGRAM_STARS_CHECKOUT_DEADLINE`). Проверка здесь
    — быстрая: заказ существует, цена совпадает. Долгое (прогон, генерация) — после
    `successful_payment`, иначе оплата сорвётся по таймауту.

    ⚠ «Да» здесь — не деньги: см. `telegram_stars_payment`.
    """
    if not ok and not error:
        return {'ok': False, 'error': 'отказ без текста: Telegram требует error_message'}

    params = {'pre_checkout_query_id': query_id, 'ok': bool(ok)}
    if not ok:
        params['error_message'] = error

    answer = await _call(token, 'answerPreCheckoutQuery', params, timeout)

    return {'ok': answer['ok'], 'error': answer['error']}


def telegram_stars_payment(update: dict) -> dict | None:
    """Разобрать апдейт об успешной оплате (`successful_payment`) — единственное подтверждение денег.

    Принимает апдейт целиком (`{'message': {…}}`) или само сообщение.

    Returns:
        `{'charge_id', 'user_id', 'chat_id', 'amount', 'currency', 'payload'}` или
        `None`, если в апдейте нет оплаты. `charge_id` — ключ для возврата и для
        идемпотентного зачисления.

    ⚠⚠ Зачислять по `charge_id` с уникальным ключом: Telegram присылает апдейт
    повторно, если бот не подтвердил его вовремя (рестарт посреди обработки).
    """
    message = (update or {}).get('message', update) or {}
    paid = message.get('successful_payment') if isinstance(message, dict) else None
    if not isinstance(paid, dict):
        return None

    return {'charge_id': str(paid.get('telegram_payment_charge_id') or ''),
            'user_id': int((message.get('from') or {}).get('id') or 0),
            'chat_id': int((message.get('chat') or {}).get('id') or 0),
            'amount': int(paid.get('total_amount') or 0),
            'currency': str(paid.get('currency') or ''),
            'payload': str(paid.get('invoice_payload') or '')}


async def telegram_stars_refund(token: str, user_id: int, charge_id: str,
                                *, timeout: float = 20.0) -> dict:
    """Вернуть звёзды за оплату пользователю — услуга не вышла, заказ отменён.

    Args:
        user_id: кто платил (`user_id` из `telegram_stars_payment`).
        charge_id: `charge_id` из `telegram_stars_payment`.

    Returns:
        `{'ok': bool, 'error': str}`.

    ⚠ Повторный возврат того же платежа Telegram отклоняет (`CHARGE_ALREADY_REFUNDED`):
    это `ok=False`, но деньги уже у пользователя — отмечайте возврат у себя по
    первому успеху и не зовите второй раз.
    """
    answer = await _call(token, 'refundStarPayment',
                         {'user_id': int(user_id), 'telegram_payment_charge_id': charge_id},
                         timeout)

    return {'ok': answer['ok'], 'error': answer['error']}


async def telegram_stars_balance(token: str, *, timeout: float = 20.0) -> dict:
    """Сколько звёзд сейчас на счету бота — сверка с собственным учётом.

    Returns:
        `{'ok': bool, 'amount': int, 'error': str}` — целых звёзд; дробная часть
        (`nanostar_amount`) отбрасывается.
    """
    answer = await _call(token, 'getMyStarBalance', {}, timeout)
    result = answer['result'] if isinstance(answer['result'], dict) else {}

    return {'ok': answer['ok'], 'amount': int(result.get('amount') or 0),
            'error': answer['error']}


async def telegram_stars_transactions(token: str, offset: int = 0, limit: int = 100,
                                      *, timeout: float = 20.0) -> dict:
    """Журнал звёзд бота: поступления и возвраты, от новых к старым — для сверки с базой.

    Args:
        offset: сколько записей пропустить (листание).
        limit: сколько вернуть, 1–100.

    Returns:
        `{'ok': bool, 'transactions': [{'id', 'amount', 'date', 'user_id',
        'kind'}], 'error': str}`; `kind` — `in` (пришло от пользователя) или `out`
        (ушло: возврат, вывод). `id` у входящей равен `charge_id` оплаты.
    """
    answer = await _call(token, 'getStarTransactions',
                         {'offset': max(0, int(offset)), 'limit': max(1, min(100, int(limit)))},
                         timeout)
    result = answer['result'] if isinstance(answer['result'], dict) else {}

    return {'ok': answer['ok'],
            'transactions': [_transaction(row) for row in result.get('transactions') or []],
            'error': answer['error']}


def main() -> None:
    """CLI: python -m telegram_.telegram_stars balance|transactions|link — токен из TELEGRAM_BOT_TOKEN."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('balance', help='звёзды на счету бота')
    p_tx = sub.add_parser('transactions', help='журнал поступлений и возвратов')
    p_tx.add_argument('--offset', type=int, default=0)
    p_tx.add_argument('--limit', type=int, default=20)
    p_link = sub.add_parser('link', help='ссылка на счёт (проверить цену и вид)')
    p_link.add_argument('title')
    p_link.add_argument('amount', type=int)
    p_link.add_argument('--description', default='')
    p_link.add_argument('--payload', default='cli-test')
    args = parser.parse_args()

    token = config_get(TELEGRAM_STARS_TOKEN_ENV, '')
    if not token:
        raise SystemExit(f'нет {TELEGRAM_STARS_TOKEN_ENV} в окружении или .env')

    if args.cmd == 'balance':
        result = asyncio.run(telegram_stars_balance(token))
    elif args.cmd == 'transactions':
        result = asyncio.run(telegram_stars_transactions(token, args.offset, args.limit))
    else:
        result = asyncio.run(telegram_stars_invoice_link(
            token, args.title, args.description or args.title, args.payload, args.amount))

    print(json_out(result))


# ── детали реализации ──────────────────────────────────────────────────────

async def _call(token: str, method: str, params: dict, timeout: float) -> dict:
    """Один вызов Bot API: `{'ok', 'result', 'error'}`, токена в тексте ошибки нет.

    ⚠ Текст ошибки собирается из `description` Bot API или типа исключения, но не
    из `str(exc)`: у httpx там адрес запроса, а в адресе — токен.
    """
    if not token:
        return {'ok': False, 'result': None, 'error': 'нет токена бота'}

    url = f'{TELEGRAM_STARS_API}/bot{token}/{method}'
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.post(url, json=params)
    except httpx.TimeoutException:
        return {'ok': False, 'result': None, 'error': f'{method}: нет ответа за {timeout:g} с'}
    except httpx.HTTPError as exc:
        return {'ok': False, 'result': None, 'error': f'{method}: {type(exc).__name__}'}

    try:
        body = response.json()
    except ValueError:
        return {'ok': False, 'result': None,
                'error': f'{method}: ответ не JSON (HTTP {response.status_code})'}

    if not body.get('ok'):
        return {'ok': False, 'result': None,
                'error': f"{method}: {body.get('description') or response.status_code}"}

    return {'ok': True, 'result': body.get('result'), 'error': ''}


def _invoice_params(title: str, description: str, payload: str, amount: int,
                    label: str, photo_url: str) -> dict:
    """Тело счёта для sendInvoice/createInvoiceLink — или `{'error'}` до похода в сеть.

    ⚠ `provider_token` не передаётся вовсе: для `XTR` он обязан быть пустым, а
    любой непустой Telegram отклоняет. Цена — ровно одна строка: несколько строк
    звёзды не принимают.
    """
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        return {'error': f'цена в звёздах — целое ≥ 1, а пришло {amount!r}'}
    size = len((payload or '').encode('utf-8'))
    if not 1 <= size <= TELEGRAM_STARS_PAYLOAD_MAX:
        return {'error': f'payload — 1–{TELEGRAM_STARS_PAYLOAD_MAX} байт, а пришло {size}'}
    if not title or not description:
        return {'error': 'у счёта нужны title и description'}

    params = {'title': title, 'description': description, 'payload': payload,
              'currency': TELEGRAM_STARS_CURRENCY,
              'prices': [{'label': label or title, 'amount': amount}]}
    if photo_url:
        params['photo_url'] = photo_url

    return params


def _transaction(row: dict) -> dict:
    """Строка журнала звёзд в плоский вид: кто, сколько, когда, в какую сторону."""
    source = row.get('source') or {}
    receiver = row.get('receiver') or {}
    party = source or receiver

    return {'id': str(row.get('id') or ''), 'amount': int(row.get('amount') or 0),
            'date': int(row.get('date') or 0),
            'user_id': int((party.get('user') or {}).get('id') or 0),
            'kind': 'in' if source else 'out'}


if __name__ == '__main__':
    main()

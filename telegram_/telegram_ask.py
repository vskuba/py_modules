"""Обмен репликами с ботом из-под живого аккаунта: спросил — дождался ответа.

Bot API на чужого бота не заведёшь, а много ботов (реестры, поиск, поддержка
сервисов) живут только в обычном чате и отвечают текстом по тексту. Здесь тот же
авторизованный клиент Telethon, что и у `telegram_session`, но роль другая: не
собирать подписчиков, а вести диалог — отправить строку и собрать всё, что бот
ответил на неё (обычно несколько сообщений: карточка, кнопки-текст, ссылки).

Порядок вызова: `telegram_ask_start` (отметка «отсюда считаем»), затем
`telegram_ask_send` (отправить и ждать). Ответ бот приходит не одним сообщением
и не сразу — ждём до `wait` секунд, но как только после первого ответа `idle`
секунд не приходит ничего нового, чат считается замолчавшим: боты реестров
додумывают long-poll'ом, а не держат соединение.

⚠ Один клиент на много запросов: сессия на аккаунте одна, а запросы к боту
дороже обычного чтения — между ними выдержка `pause`, иначе аккаунт ловит
флуд-вайлит и ответы перестают приходить вовсе.
"""

import argparse
import asyncio
import json
from pathlib import Path

from config.config import config_get

# Сколько ждать ответа в целом и сколько тишины считать концом ответа. Реестровый
# бот на свободном тарифе отвечает за секунды, но карточка из нескольких сообщений
# доезжает рывками; 4 секунды тишины после первого сообщения — уже ответ закончен.
TELEGRAM_ASK_WAIT = 40.0
TELEGRAM_ASK_IDLE = 4.0
TELEGRAM_ASK_PAUSE = 3.0


async def telegram_ask_start(client, bot: str) -> int:
    """Вернуть отметку «отсюда считаем»: id последнего сообщения в чате с ботом."""
    return await _telegram_ask_last_id(client, bot)


async def telegram_ask_send(client, bot: str, text: str, wait: float = TELEGRAM_ASK_WAIT,
                            idle: float = TELEGRAM_ASK_IDLE, mark: int = 0,
                            cap: int = 40) -> dict:
    """Отправить `text` боту и собрать его ответ; вернёт `{ok, replies, error}`.

    `replies` — [{date, text}] строго новые (после `mark`; `mark` = 0 — считать от
    момента вызова). Тишина `idle` после первого нового сообщения = ответ сошёлся;
    за `wait` не пришло ничего — `{ok: False, error: 'нет ответа'}`: это не поломка
    транспорта, бот мог уйти в rate-limit — решение читающему.

    Args:
        client: авторизованный клиент из `telegram_session_start`.
        bot: username бота без @.
        text: строка запроса.
        wait: сколько секунд ждать ответ всего.
        idle: сколько секунд тишины после первого сообщения считать ответ законченным.
        mark: id, от которого считать новые сообщения (см. `telegram_ask_start`).
        cap: максимум собрать сообщений за один обмен.
    """
    chat = await client.get_entity(bot)
    sent = await client.send_message(chat, text)
    since = max(mark, sent.id)
    replies: list[dict] = []
    last_hit = asyncio.get_running_loop().time()
    deadline = last_hit + wait
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2)
        ids = [m.id async for m in client.iter_messages(chat, limit=cap, min_id=since)]
        if ids:
            new = []
            async for m in client.iter_messages(chat, ids=sorted(ids)):
                new.append({'id': m.id, 'date': str(m.date), 'text': m.text or ''})
            fresh = [r for r in new if not any(r['id'] == s['id'] for s in replies)]
            if fresh:
                replies.extend(fresh)
                last_hit = asyncio.get_running_loop().time()
        if replies and asyncio.get_running_loop().time() - last_hit >= idle:
            break
    replies.sort(key=lambda r: r['id'])
    return {'ok': bool(replies), 'replies': replies,
            'error': '' if replies else 'нет ответа'}


async def _telegram_ask_last_id(client, bot: str) -> int:
    chat = await client.get_entity(bot)
    async for m in client.iter_messages(chat, limit=1):
        return m.id
    return 0


async def _run(bot: str, queries: list[str], out_dir: str,
               pause: float = TELEGRAM_ASK_PAUSE) -> dict:
    from telegram_.telegram_session import telegram_session_start

    client = await telegram_session_start(
        config_get('TELEGRAM_PHONE'), int(config_get('TELEGRAM_API_ID')),
        config_get('TELEGRAM_API_HASH'), config_get('TELEGRAM_SESSION'))
    done = []
    try:
        for query in queries:
            replies = await telegram_ask_send(client, bot, query)
            if out_dir:
                name = ''.join(c if c.isalnum() else '_' for c in query)[:60]
                Path(out_dir).mkdir(parents=True, exist_ok=True)
                Path(out_dir, f'{name}.json').write_text(
                    json.dumps({'query': query, **replies}, ensure_ascii=False, indent=1),
                    encoding='utf-8')
            print(f"{'ok ' if replies['ok'] else '!! '} {query!r}: "
                  f"{len(replies['replies'])} репл.", flush=True)
            done.append({'query': query, 'ok': replies['ok'], 'replies': replies['replies']})
            await asyncio.sleep(pause)
    finally:
        await client.disconnect()
    return {'asked': len(done), 'silent': sum(1 for d in done if not d['ok'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='обмен запросами с ботом из-под аккаунта')
    parser.add_argument('--bot', default='opendatauabot', help='username бота без @')
    parser.add_argument('--query', action='append', default=[], help='строка запроса, повторно для нескольких')
    parser.add_argument('--file', default='', help='файл с запросами, по строке на запрос')
    parser.add_argument('--out-dir', default='', help='папка для ответов (json на запрос)')
    parser.add_argument('--pause', type=float, default=TELEGRAM_ASK_PAUSE)
    args = parser.parse_args()
    qs = args.query + (Path(args.file).read_text(encoding='utf-8').splitlines()
                       if args.file else [])
    summary = asyncio.run(_run(args.bot, [q for q in qs if q.strip()], args.out_dir, args.pause))
    print(f"запросов {summary['asked']}, без ответа {summary['silent']}")

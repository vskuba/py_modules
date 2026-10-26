# Публичные превью telegram: лента канала без токена

> Публичный канал открывается обычным HTTP: `t.me/s/<channel>` — тот же
> текст, что видит вошедший, без аккаунта, бота и следов в подписчиках.
> Читаем даты постов, ссылки, упоминания и дату заведения канала.

## Как

```python
from telegram_.telegram_public import telegram_public_info, telegram_public_posts

info = telegram_public_info('durov')        # {'title','subscribers','created',…}
feed = telegram_public_posts('durov', pages=3)
for p in feed['posts']:
    p['ts'], p['text'], p['links'], p['mentions']
# feed['next'] — id, с которого листать дальше при следующем походе
```

`pages` — по ~20 постов за страницу; листай `before=feed['next']`, пока
`posts` не опустеет. Даты — UTC, `text` — с разбором `<br>` и ссылок.

```bash
PYTHONPATH=. python -m telegram_.telegram_public durov --pages 2
```

## Грабли

1. **Приватный канал неотличим от опечатки глазами** — обе дают заглушку без
   `tgme_widget_message`; модуль возвращает `ok=False` с этим разбором.
2. **Плашки «Channel created» на странице может не быть** — `created=''` не
   значит, что канал без даты; косвенный потолок даёт `ts` самого старого
   дошедшего поста.
3. **~20 постов на страницу и никаких намёков на лимит** — `pages>10`
   превращаются в вежливый timeout; за старьём листай `before` с паузами.

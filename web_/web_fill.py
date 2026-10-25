"""Отпечаток наполнения сайта: жив магазин или застыл — по товарам, не по HTML.

Вопрос, на который отвечает модуль: «дышит ли площадка?» — не разово, а в
динамике. Магазин живёт, когда в нём двигается содержимое: появляются позиции,
уходят распроданные, ползут цены. Раз в неделю зайти и посмотреть — не ответ:
нужна память между походами, и она здесь в базе.

## Что НЕ сравниваем

Не HTML и не скриншот: страница магазина наполовину состоит из шума — таймеры
акций, «вы смотрели», карусель рекомендаций, порядок которых меняется от
загрузки к загрузке. Сравнивать разметку — значит находить перемену каждую
ночь и на мёртвом сайте тоже. Здесь сравниваются **только переданные поля
товара**: идентификатор (URL без параметров), цена, наличие. Часы в шапке в
этот набор не попадают ни при каком раскладе.

## Как устроено сравнение

Вызов `web_fill_note` получает срез витрины — список позиций, — и сверяет его
с тем, что помнит о `name` (площадке) с прошлых срезов:

* позиция впервые — `новые`; исчезла из полного среза — `ушли`;
* сдвинулась цена или наличие — `изменились`;
* цифровой след среза (`digest`) — стабильный хэш отсортированных кортежей:
  порядок выдачи на него не влияет, сортировка лечит и этот источник шума.

`web_fill_report` складывает дневники за окно и выносит приговор: `live` —
содержимое двигалось недавно, `frozen` — тишина дольше порога (склад заброшен,
и заказы туда лучше не вести), `empty` — срезов не было вовсе.

## Хранилище

Таблицы семьи `web_fill_*` — их заводит проект у себя миграцией (договор как у
`browser_*`: модуль знает имена, схему сеет проект):

```sql
CREATE TABLE IF NOT EXISTS `web_fill_item` (
    `name`        VARCHAR(190) NOT NULL,
    `item_id`     VARCHAR(190) NOT NULL,
    `price`       BIGINT       NULL COMMENT 'копейки/центы; NULL — цена не известна',
    `in_stock`    TINYINT      NULL COMMENT '1 есть, 0 нет, NULL — не сказано',
    `first_seen`  DATE         NOT NULL,
    `last_seen`   DATE         NOT NULL,
    `seen_days`   INT          NOT NULL DEFAULT 1,
    PRIMARY KEY (`name`, `item_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `web_fill_log` (
    `id`        BIGINT AUTO_INCREMENT PRIMARY KEY,
    `name`      VARCHAR(190) NOT NULL,
    `taken_at`  DATETIME     NOT NULL,
    `n_items`   INT          NOT NULL,
    `n_new`     INT          NOT NULL,
    `n_changed` INT          NOT NULL,
    `n_gone`    INT          NOT NULL,
    `whole`     TINYINT      NOT NULL DEFAULT 1,
    `digest`    CHAR(16)     NOT NULL,
    KEY `by_name` (`name`, `taken_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

## ⚠ Срез полный или частичный

`whole=False` — срез видит **часть** витрины (одну категорию из ста): исчезнувшие
позиции при таком срезе не считаются — иначе каждая выборочная прогулка
докладывала бы фантомные «ушли» ровно про то, что в этот раз не смотрели.
`n_gone` имеет смысл только у `whole=True`.

* ⚠⚠ Идентификатор позиции нормализуется: схема, `www`, регистр хоста, хвостовой
  слэш и **вся query-строка** выбрасываются. `?utm=…` — тот же товар, и без
  нормализации каждая рекламная ссылка рождала бы новый товар. Площадки, у
  которых товар различается параметром (редкость), передают `id` сами — тогда
  нормализация к URL не применяется вовсе.
* ⚠ Цену сравниваем в целых сотых (`round(p*100)`): float «1999.9999999» и
  «2000.0000001» — одна цена, а float-равенство доложило бы о смене каждый срез.
* ⚠ `taken_at` — часы вызывающей машины. Между проектами с разным временем
  сдвиг на часы допустим: приговор смотрит на дни, а не на часы.
"""

import hashlib
import json
import sys
import urllib.parse
from datetime import datetime

from logging_.logging_ import logger_info
from mysql_.mysql_ import mysql_get_db_async

# Дней молчания, после которых склад объявляется заброшенным. Две недели —
# компромисс: витрины refresh'ят еженедельными пачками, и месяц без единого
# движения значит, что двигать там уже нечего.
WEB_FILL_FROZEN_DAYS = 14

# Сколько строк дневника поднимает отчёт за раз. Срезы частые, но и за год их
# набегает тысячи; агрегаты считает база, а в память идёт столько, сколько
# человек способен прочесть.
WEB_FILL_LOG_ROWS = 400


async def web_fill_note(name: str, items, whole: bool = True,
                        when: datetime | None = None) -> dict:
    """Запомнить срез витрины и доложить, чем он отличается от памяти.

    Args:
        name: ключ площадки (домен, магазин — тот же, что в отчётах).
        items: позиции; каждая — dict `{'url'|'id', 'price'?, 'in_stock'?}`
            либо кортеж `(url_или_id, цена, наличие)`.
        whole: полный ли срез; при `False` исчезнувшие не считаются (см. докстринг модуля).
        when: момент среза; умолчание — часы вызывающей машины.

    Returns:
        dict: `n` — позиций в срезе, `new` / `changed` / `gone` — движения,
        `whole` — как посчитан срез, `digest` — цифровой след (для сверки
        двух срезов между собой без чтения памяти).

    Raises:
        ValueError: пустой `name` или ни одной позиции без идентификатора.
    """
    if not str(name).strip():
        raise ValueError('web_fill_note: пустой name — под ним срез и запомнится')

    now = when or datetime.now()
    today = now.date()

    current: dict[str, tuple] = {}
    for item in items or ():
        if isinstance(item, dict):
            key = str(item.get('id') or item.get('url') or '').strip()
            price, stock = item.get('price'), item.get('in_stock')
            if not item.get('id'):
                key = web_fill_item_id(key)
        else:
            key, price, stock = (list(item) + [None, None])[:3]
            key = str(key).strip()
            if not isinstance(item, (list, tuple)) or _looks_url(key):
                key = web_fill_item_id(key)
        if not key:
            raise ValueError(f'web_fill_note: у позиции нет ни url, ни id: {item!r}')
        current[key] = (None if price is None else int(round(float(price) * 100)),
                        None if stock is None else (1 if stock else 0))

    pairs = sorted((key, price, stock) for key, (price, stock) in current.items())
    digest = hashlib.sha256('|'.join(
        f'{k}~{"" if p is None else p}~{"" if s is None else s}' for k, p, s in pairs
    ).encode('utf-8')).hexdigest()[:16]

    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT `item_id`, `price`, `in_stock`, `first_seen` FROM `web_fill_item`'
            ' WHERE `name` = %s', (str(name), ))
        memory = {row['item_id']: row for row in await db.fetchall() or []}

        fresh = sum(1 for key in current if key not in memory)
        moved = sum(1 for key, (price, stock) in current.items()
                    if key in memory and (
                        _cents(memory[key]['price']) != price
                        or _flag(memory[key]['in_stock']) != stock))
        vanished = 0
        if whole:
            vanished = sum(1 for key, row in memory.items()
                           if key not in current and row['first_seen'] <= today)

        if current:
            await db.executemany(
                'INSERT INTO `web_fill_item` (`name`, `item_id`, `price`, `in_stock`,'
                ' `first_seen`, `last_seen`, `seen_days`) VALUES (%s, %s, %s, %s, %s, %s, 1)'
                ' ON DUPLICATE KEY UPDATE `price` = VALUES(`price`),'
                ' `in_stock` = VALUES(`in_stock`),'
                ' `last_seen` = GREATEST(`last_seen`, VALUES(`last_seen`)),'
                ' `seen_days` = `seen_days` + (`last_seen` < VALUES(`last_seen`))',
                [(str(name), key, price, stock, today, today)
                 for key, (price, stock) in current.items()])

        await db.execute(
            'INSERT INTO `web_fill_log` (`name`, `taken_at`, `n_items`, `n_new`,'
            ' `n_changed`, `n_gone`, `whole`, `digest`)'
            ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
            (str(name), now, len(current), fresh, moved, vanished, 1 if whole else 0, digest))

    logger_info(f'[наполнение] {name}: {len(current)} позиций,'
                f' новых {fresh}, сдвигов {moved}, ушло {vanished}')
    return {'n': len(current), 'new': fresh, 'changed': moved, 'gone': vanished,
            'whole': whole, 'digest': digest}


async def web_fill_report(name: str, days: int = 30) -> dict:
    """Приговор площадке по памяти наполнения за окно.

    Args:
        name: тот же ключ, под которым клали срезы.
        days: глубина окна в днях.

    Returns:
        dict: `status` — `live` (содержимое двигалось), `frozen` (тишина дольше
        `WEB_FILL_FROZEN_DAYS`) или `empty` (срезов не было); `n_items` — сколько
        позиции помнят вид за окно; `new` / `changed` / `gone` — суммы движений;
        `new_day` — позиции, впервые увиденные за последние сутки;
        `since_move` — дней с последнего движения (None, если движения не было);
        `first` — самый ранний срез; `last` — последний.

    ⚠ `new` — сумма событий первого появления за окно, а не «сколько нового
    сейчас»: позиция рождается новой ровно однажды, и пока площадка моложе
    окна, `new` тождественно равняется `n_items` — это свойство метрики, а не
    витрины. На вопрос «что нового теперь?» отвечает `new_day`.
    """
    async with mysql_get_db_async() as db:
        await db.execute(
            'SELECT COUNT(*) n, MIN(first_seen) first_seen,'
            ' SUM(`first_seen` >= NOW() - INTERVAL 1 DAY) new_day'
            ' FROM `web_fill_item`'
            ' WHERE `name` = %s AND `last_seen` >= CURDATE() - INTERVAL %s DAY',
            (str(name), int(days)))
        catalog = await db.fetchone() or {}
        await db.execute(
            'SELECT COUNT(*) n, MIN(taken_at) first_at, MAX(taken_at) last_at,'
            ' SUM(`n_new`) new, SUM(`n_changed`) changed, SUM(`n_gone`) gone,'
            ' MAX(CASE WHEN `n_new` + `n_changed` + (`whole` * `n_gone`) > 0'
            '     THEN `taken_at` END) moved_at'
            ' FROM `web_fill_log` WHERE `name` = %s AND `taken_at` >= NOW() - INTERVAL %s DAY',
            (str(name), int(days)))
        diary = await db.fetchone() or {}

    if not diary.get('n'):
        return {'status': 'empty', 'n_items': int(catalog.get('n') or 0),
                'new_day': int(catalog.get('new_day') or 0)}

    moved_at = diary.get('moved_at')
    # Целые сутки, а не `.days`: тот у отрицательного остатка (срез только что
    # снят, часы разошлись на секунду) даёт −1, и «сдвиг был» читался бы как
    # «завтра».
    since_move = (max(0, int((datetime.now() - moved_at).total_seconds() // 86400))
                  if moved_at else None)
    status = ('live' if since_move is not None and since_move < WEB_FILL_FROZEN_DAYS
              else 'frozen')
    return {
        'status': status,
        'n_items': int(catalog.get('n') or 0),
        'new': int(diary.get('new') or 0),
        'new_day': int(catalog.get('new_day') or 0),
        'changed': int(diary.get('changed') or 0),
        'gone': int(diary.get('gone') or 0),
        'first': str(diary.get('first_at') or ''),
        'last': str(diary.get('last_at') or ''),
        'since_move': since_move,
        'notes': int(diary.get('n') or 0),
    }


def web_fill_item_id(url_or_id: str) -> str:
    """Канонический идентификатор позиции из адреса.

    Выбрасывает схему, `www`, хвостовой слэш и **параметры целиком**: одна и та
    же карточка с `?utm_source=…` и без — один и тот же товар, и считать её
    новым при каждой ссылке значит обесценить весь отчёт.
    """
    raw = str(url_or_id).strip()
    if '://' not in raw and '/' not in raw and '?' not in raw:
        return raw  # это и есть id, не трогаем
    parts = urllib.parse.urlsplit(raw if '://' in raw else 'http://' + raw)
    host = parts.netloc.lower().removeprefix('www.')
    path = parts.path.rstrip('/')
    return host + path


def _looks_url(key: str) -> bool:
    return '://' in key or key.count('/') > 1 or '.' in key.split('/')[0]


def _cents(value) -> int | None:
    return None if value is None else int(value)


def _flag(value) -> int | None:
    return None if value is None else int(value)


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description='Отпечаток наполнения витрины: note — запомнить срез, report — приговор.')
    sub = parser.add_subparsers(dest='mode', required=True)

    p_note = sub.add_parser('note', help='запомнить срез из JSON-файла (- = stdin)')
    p_note.add_argument('--name', required=True, help='ключ площадки')
    p_note.add_argument('--file', required=True, help='позиции: [{url|id, price?, in_stock?}]')
    p_note.add_argument('--part', action='store_true', help='срез частичный, «ушли» не считать')

    p_report = sub.add_parser('report', help='что помнит база о площадке')
    p_report.add_argument('--name', required=True)
    p_report.add_argument('--days', type=int, default=30)

    args = parser.parse_args()

    if args.mode == 'note':
        raw = sys.stdin.read() if args.file == '-' else open(args.file, encoding='utf-8').read()
        result = asyncio.run(web_fill_note(args.name, json.loads(raw), whole=not args.part))
    else:
        result = asyncio.run(web_fill_report(args.name, days=args.days))
    print(json.dumps(result, ensure_ascii=False, default=str, indent=1))

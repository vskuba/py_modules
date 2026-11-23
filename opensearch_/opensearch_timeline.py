"""
Лента логов одной строкой на запись: `время под уровень сообщение` — разбор инцидента по поду.

Родилась на разборе DLQ консьюмера (2026-11): чтобы увидеть, что консьюмер сделал
за секунду до падения, NDJSON из `opensearch_ search` шесть раз прогонялся через
самописный jq — срезать SQL-шум, склеить задвоенные строки, достать stderr-трейс.
Здесь то же одним вызовом.

## Что видно и что спрятано

* Строка: `HH:MM:SS.mmm  <хвост пода>  <уровень>  <сообщение в одну строку>`.
  Хвост пода — последние 5 символов `resource.labels.pod_name` (суффикс реплики
  k8s), этого хватает различить реплики одного деплоймента.
* **SQL-шум DEBUG спрятан** (`Executing statement`, `Beginning/Committing
  transaction`); `Rolling back transaction` оставлен — это след ошибки.
  `--sql` возвращает всё.
* **Задвоенные строки склеены**: одна и та же запись в долгоживущем консьюмере
  может прийти дважды (разные `insertId`, одинаковые время и текст).
* **stderr-трейсы видны**: у вывода консоли нет `jsonPayload`, текст лежит в
  `textPayload` — уровень печатается `-`. Именно там `In EntityManagerClosed.php`
  и прочий финал упавшей команды; пустые строки трейса выкинуты.
* `--not-info` оставляет только NOTICE и выше плюс stderr — «что пошло не так».

## Фильтры

`--pod`, `--controller` (`labels.logging.gke.io/top_level_controller_name`),
`--trace` (`jsonPayload.traceId`) — сокращения над `--filter FIELD=VALUE`
(match_phrase), его можно добавлять сколько угодно. Окно — `--time-from/--time-to`
по полю `timestamp`.

CLI:

    # что делал под консьюмера за 3 минуты вокруг падения
    python -m opensearch_.opensearch_timeline 'myapp-prod-app-*' \\
        --pod app-daemon-consumer.check-statusmxzhf \\
        --time-from 2026-11-17T12:11:00Z --time-to 2026-11-17T12:13:00Z

    # все реплики деплоймента, только проблемы
    python -m opensearch_.opensearch_timeline 'myapp-prod-app-*' \\
        --controller app-daemon-consumer.check-status \\
        --time-from 2026-11-17T12:00:00Z --time-to 2026-11-17T12:20:00Z --not-info

⚠ PCI: в сообщениях бывают маски, телефоны и PAN — вывод в /tmp, не в git.
"""

from __future__ import annotations

import argparse
import re

from opensearch_.opensearch_ import DEFAULT_ENV_FILE, opensearch_env, opensearch_hits

OPENSEARCH_TIMELINE_FIELDS = ("timestamp,resource.labels.pod_name,jsonPayload.logLevel,"
                              "jsonPayload.message,textPayload")
OPENSEARCH_TIMELINE_SQL_RE = re.compile(
    r"^(Executing (prepared )?statement|Executing query|Beginning transaction|Committing transaction)")
OPENSEARCH_TIMELINE_QUIET_LEVELS = ("INFO", "DEBUG")
OPENSEARCH_TIMELINE_POD_TAIL = 5
OPENSEARCH_TIMELINE_FILTER_ALIASES = {
    "pod": "resource.labels.pod_name",
    "controller": "labels.logging.gke.io/top_level_controller_name",
    "trace": "jsonPayload.traceId",
}


def opensearch_timeline(index: str, filters=(), time_from: str | None = None,
                        time_to: str | None = None, size: int = 500, query: str = "all",
                        sql: bool = False, not_info: bool = False,
                        env: dict | None = None) -> list:
    """Записи окна строками ленты (см. `opensearch_timeline_rows`), по времени вверх.

    Args:
        index: паттерн индекса (`myapp-prod-app-*`).
        filters: `FIELD=VALUE` (match_phrase), как у `opensearch_ search --filter`.
        time_from, time_to: ISO-границы по `timestamp`.
        size: сколько записей забрать до фильтрации (лимит кластера — 10000).
        query: `all`, JSON clause или строка Lucene.
        sql: True — не прятать DEBUG SQL.
        not_info: True — только NOTICE+ и stderr.

    Returns:
        [{'ts','pod','level','message'}].
    """
    hits = opensearch_hits(index, query, env or opensearch_env(), filter=list(filters),
                           sort="timestamp", size=size, fields=OPENSEARCH_TIMELINE_FIELDS,
                           time_from=time_from, time_to=time_to)
    return opensearch_timeline_rows(hits, sql=sql, not_info=not_info)


def opensearch_timeline_rows(hits: list, sql: bool = False, not_info: bool = False) -> list:
    """Хиты `opensearch_hits` → строки ленты: шум срезан, дубли склеены.

    Args:
        hits: [{'ts', '_source'}] в порядке времени.
        sql: True — оставить SQL-шум DEBUG.
        not_info: True — выкинуть INFO/DEBUG (stderr-строки остаются).

    Returns:
        [{'ts','pod','level','message'}]; level `-` — строка stderr (textPayload).
    """
    rows, seen = [], set()
    for hit in hits:
        row = _row(hit)
        if not row["message"].strip():
            continue
        if not sql and OPENSEARCH_TIMELINE_SQL_RE.match(row["message"]):
            continue
        if not_info and row["level"] in OPENSEARCH_TIMELINE_QUIET_LEVELS:
            continue
        key = (row["ts"], row["pod"], row["level"], row["message"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def opensearch_timeline_format(row: dict, width: int = 300) -> str:
    """Строка ленты: `HH:MM:SS.mmm  pod  LEVEL  message` (сообщение режется до width)."""
    ts = str(row["ts"])
    clock = ts[11:23] if len(ts) >= 23 else ts
    return f"{clock}  {row['pod']:<5}  {row['level']:<8} {row['message'][:width]}"


def _row(hit: dict) -> dict:
    """Хит → {'ts','pod','level','message'}: jsonPayload или textPayload stderr."""
    src = hit.get("_source") or {}
    payload = src.get("jsonPayload") or {}
    pod = (((src.get("resource") or {}).get("labels") or {}).get("pod_name") or "")
    if payload:
        level, message = payload.get("logLevel") or "?", payload.get("message") or ""
    else:
        level, message = "-", src.get("textPayload") or ""
    return {"ts": hit.get("ts") or src.get("timestamp") or "",
            "pod": pod[-OPENSEARCH_TIMELINE_POD_TAIL:],
            "level": level,
            "message": " ".join(str(message).split())}


def _main() -> None:
    ap = argparse.ArgumentParser(prog="opensearch_timeline", description=__doc__.splitlines()[1])
    ap.add_argument("index", help="паттерн индекса, напр. myapp-prod-app-*")
    ap.add_argument("query", nargs="?", default="all", help="all | JSON | строка Lucene")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                    help="env-файл; по умолчанию .env.local проекта, файла нет — окружение процесса")
    ap.add_argument("--filter", action="append", default=[], metavar="FIELD=VALUE")
    for alias, field in OPENSEARCH_TIMELINE_FILTER_ALIASES.items():
        ap.add_argument(f"--{alias}", help=f"= --filter {field}=…")
    ap.add_argument("--time-from", help="ISO, напр 2026-11-17T12:11:00Z")
    ap.add_argument("--time-to", help="ISO")
    ap.add_argument("--size", type=int, default=500)
    ap.add_argument("--width", type=int, default=300, help="обрезка сообщения, символов")
    ap.add_argument("--sql", action="store_true", help="не прятать DEBUG SQL")
    ap.add_argument("--not-info", action="store_true", help="только NOTICE+ и stderr")
    args = ap.parse_args()
    filters = list(args.filter)
    for alias, field in OPENSEARCH_TIMELINE_FILTER_ALIASES.items():
        if getattr(args, alias):
            filters.append(f"{field}={getattr(args, alias)}")
    rows = opensearch_timeline(args.index, filters, args.time_from, args.time_to, args.size,
                               args.query, args.sql, args.not_info,
                               opensearch_env(args.env_file))
    for row in rows:
        print(opensearch_timeline_format(row, args.width))
    print(f"строк: {len(rows)}")


if __name__ == "__main__":
    _main()

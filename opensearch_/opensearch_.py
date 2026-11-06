"""
OpenSearch за Google SSO: запросы через Dashboards по SAML-сессии, без REST-кредов.

Модуль родился на выладке (2026-11), где REST кластера оказался закрыт извне, а
вход — только SAML через Google. Карта ловушек — в docstring, она половина
ценности модуля.

## Карта ловушек (проверено на живом opensearch.devopdata.co, OSD 3.9)

* **REST закрыт, `api/console/proxy` тоже (404).** Единственный живой путь к
  данным — `POST /internal/search/<стратегия>`. Стратегию угадать нельзя:
  типовой `opensearch-dashboards` отвечает «Search strategy not found» (500),
  а рабочий id этой установки — `opensearch-with-long-numerals`. Ошибку путают
  с отказом авторизации — это найденный эндпоинт, просто не та стратегия.
* **Нужны ОБА cookie**: `security_authentication` и `security_authentication_saml1`.
  С одним первым — 401 и принудительное затирание (`Set-Cookie …Max-Age=0`),
  что выглядит как «поддельный cookie», а на деле — нет второй половины сессии.
* **Ротация**: сервер выдаёт новый `security_authentication` в ответе, старое
  значение со временем умирает. Модуль подхватывает `Set-Cookie` и пишет
  обновление в env-файл — сессия живёт своей жизнью, руками не трогать.
* **`/api/authtypes`, `api/authtoken/keep_alive` — 404**: в этой версии OSD их
  нет, продлевать сессию нечем. Не искать.
* **401 вида `{"statusCode":401}` приходит от прокладки (Node/nginx), а не от
  кластера** — по формату тела отличай: ответ кластера содержит
  `error.type`/`status`.
* **Сессия умирает вместе с TTL SAML-асерции** (часы, не дни). Признак смерти:
  снова 401 на всё. Лечение: браузер → F12 → Network → любой
  `/internal/search/`-запрос → правой кнопкой → Copy as cURL → значения двух
  cookie в env-файл (файл модуль обновляет сам, см. выше — но только когда
  сессия ещё жива).

## Что здесь есть

* `opensearch_env` — конфиг из env-файла (`.env.local` cashier-проекта).
* `opensearch_search` — POST в `/internal/search/...`, отдаёт `rawResponse`,
  по пути переписывает протировавшиеся cookie в env-файл.
* `opensearch_hits` — хиты одним списком (index/id/ts/_source), удобно в pipe.
* `opensearch_count` — только число попаданий.

CLI:

    # 300 свежих логов по traceId (сортировка по времени вверх)
    python -m opensearch_.opensearch_ search 'cashier-prod-fpm-*' all \\
        --filter jsonPayload.traceId=4cba6a6a-... --sort timestamp --size 300

    # только счётчик
    python -m opensearch_.opensearch_ count 'cashier-prod-fpm-*' all \\
        --filter jsonPayload.traceId=4cba6a6a-...

    # свой JSON целиком (body _search)
    python -m opensearch_.opensearch_ search 'cashier-prod-fpm-*' '{"aggs": …}' --raw

⚠ ВНИМАНИЕ (PCI): в `jsonPayload.message` кассира чужие данные — маски, телефоны,
полные PAN. wynik'ы держать в /tmp, в git/файлы репозитория не выгружать.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import ssl
import sys
import urllib.error
import urllib.request

DEFAULT_ENV_FILE = "/var/www/cashier-service-payment-gateway/.env.local"
DEFAULT_STRATEGY = os.environ.get("OPENSEARCH_STRATEGY", "opensearch-with-long-numerals")
UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:159.0) Gecko/20100101 Firefox/159.0"


def opensearch_env(env_file: str = DEFAULT_ENV_FILE) -> dict:
    """Конфиг доступа из env-файла: url, пара cookie, стратегия."""
    p = pathlib.Path(env_file)
    src = os.environ if not p.is_file() else {}
    kv = dict(src)
    if p.is_file():
        for line in p.read_text().splitlines():
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
            if m:
                kv[m.group(1)] = m.group(2)
    env = {
        "file": str(p),
        "url": kv.get("OPENSEARCH_URL", "https://opensearch.devopdata.co").rstrip("/"),
        "strategy": kv.get("OPENSEARCH_STRATEGY", DEFAULT_STRATEGY),
        "cookies": {
            "security_authentication": kv.get("OPENSEARCH_COOKIE", ""),
            "security_authentication_saml1": kv.get("OPENSEARCH_COOKIE_SAML", ""),
        },
    }
    if not env["cookies"]["security_authentication"]:
        raise SystemExit(f"OPENSEARCH_COOKIE пуст в {env_file}: сессия не заведена, "
                         "см. docstring модуля (Copy as cURL из браузера)")
    return env


def _persist_cookies(env: dict, fresh: dict) -> None:
    """Записать протировавшиеся cookie обратно в env-файл (файл 0600, секреты не в stdout)."""
    p = pathlib.Path(env["file"])
    if not p.is_file():
        return
    key_by_name = {"security_authentication": "OPENSEARCH_COOKIE",
                   "security_authentication_saml1": "OPENSEARCH_COOKIE_SAML"}
    text = p.read_text()
    changed = False
    for name, var in key_by_name.items():
        new = fresh.get(name)
        if new and new != env["cookies"].get(name):
            env["cookies"][name] = new
            text, n = re.subn(rf"^{var}=.*$", f"{var}={new}", text, count=1, flags=re.M)
            changed = changed or n > 0
    if changed:
        p.write_text(text)


def opensearch_search(body: dict, env: dict | None = None, timeout: int = 90) -> dict:
    """POST запроса в стратегию поиска; отдаёт rawResponse кластера.

    Тело — уже собранный `{"params": {"index": …, "body": {…}}}`. Cookie-ротация
    обрабатывается молча: свежее значение из ответа уходит в env-файл.
    """
    env = env or opensearch_env()
    url = f"{env['url']}/internal/search/{env['strategy']}"
    cookie = "; ".join(f"{k}={v}" for k, v in env["cookies"].items() if v)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": UA,
        "osd-xsrf": "osd-fetch",
        "osd-version": "3.9.0",
        "Origin": env["url"],
        "Referer": f"{env['url']}/app/data-explorer/discover",
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            set_cookies = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        set_cookies = e.headers.get_all("Set-Cookie") or []
        body_txt = e.read().decode(errors="replace")[:400]
        _persist_cookies(env, _parse_set_cookie(set_cookies))
        raise SystemExit(f"HTTP {e.code} от OpenSearch Dashboards: {body_txt}"
                         + (" — сессия мертва, см. docstring (Copy as cURL)" if e.code == 401 else ""))
    _persist_cookies(env, _parse_set_cookie(set_cookies))
    doc = json.loads(raw)
    if "rawResponse" not in doc:
        raise SystemExit(f"неожиданный ответ (нет rawResponse): {json.dumps(doc)[:400]}")
    return doc["rawResponse"]


def _parse_set_cookie(headers: list) -> dict:
    fresh = {}
    for line in headers:
        m = re.match(r"^(security_authentication(?:_saml1)?)=([^;]*)", line)
        if m and m.group(2):
            fresh[m.group(1)] = m.group(2)
    return fresh


def _build_body(index: str, query: str, args) -> dict:
    if query.strip() in ("all", "*"):
        q: dict = {"match_all": {}}
    else:
        q = json.loads(query)
    filters: list = [q]
    for f in args.filter or []:
        k, _, v = f.partition("=")
        filters.append({"match_phrase": {k: v}})
    if args.time_from or args.time_to:
        rng = {}
        if args.time_from:
            rng["gte"] = args.time_from
        if args.time_to:
            rng["lte"] = args.time_to
        filters.append({"range": {args.time_field: {**rng, "format": "strict_date_optional_time"}}})
    body: dict = {"query": {"bool": {"filter": filters}} if len(filters) > 1 else q,
                  "size": args.size, "version": True}
    if args.sort:
        order = "desc" if args.sort.startswith("-") else "asc"
        body["sort"] = [{args.sort.lstrip("-"): {"order": order, "unmapped_type": "boolean"}}]
    if args.fields:
        body["_source"] = {"includes": args.fields.split(",")}
    return {"params": {"index": index, "body": body}}


def opensearch_count(index: str, query: str = "all", env: dict | None = None, **kw) -> int:
    """Число документов, подходящих под запрос."""
    ns = argparse.Namespace(filter=kw.get("filter"), sort=None, size=0,
                            fields=None, time_field=kw.get("time_field", "timestamp"),
                            time_from=kw.get("time_from"), time_to=kw.get("time_to"))
    r = opensearch_search(_build_body(index, query, ns), env)
    return r["hits"]["total"]["value"] if isinstance(r["hits"]["total"], dict) else r["hits"]["total"]


def opensearch_hits(index: str, query: str = "all", env: dict | None = None, **kw) -> list:
    """Хиты списком [{index,id,ts,_source}] — для pipes и агрегаций в памяти."""
    ns = argparse.Namespace(filter=kw.get("filter"), sort=kw.get("sort", "timestamp"),
                            size=kw.get("size", 100), fields=kw.get("fields"),
                            time_field=kw.get("time_field", "timestamp"),
                            time_from=kw.get("time_from"), time_to=kw.get("time_to"))
    r = opensearch_search(_build_body(index, query, ns), env)
    return [{"index": h["_index"], "id": h["_id"],
             "ts": h["_source"].get("timestamp") or h["_source"].get("@timestamp"),
             "_source": h["_source"]} for h in r["hits"]["hits"]]


def main() -> None:
    ap = argparse.ArgumentParser(prog="opensearch_", description=__doc__.splitlines()[1])
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("index", help="паттерн индекса, напр. cashier-prod-fpm-*")
        sp.add_argument("query", help="all | JSON запроса (query_clause)")
        sp.add_argument("--filter", action="append", metavar="FIELD=VALUE",
                        help="match_phrase, повторяемый")
        sp.add_argument("--time-field", default="timestamp")
        sp.add_argument("--time-from", help="ISO, напр 2026-11-01T00:00:00Z")
        sp.add_argument("--time-to", help="ISO")

    sp = sub.add_parser("search")
    common(sp)
    sp.add_argument("--size", type=int, default=100)
    sp.add_argument("--sort", help="поле; -префикс = desc")
    sp.add_argument("--fields", help="csv list inclusion в _source")
    sp.add_argument("--raw", action="store_true", help="сырой rawResponse вместо NDJSON")
    spc = sub.add_parser("count")
    common(spc)

    args = ap.parse_args()
    env = opensearch_env(args.env_file)
    if args.cmd == "count":
        print(opensearch_count(args.index, args.query, env, filter=args.filter,
                               time_field=args.time_field, time_from=args.time_from,
                               time_to=args.time_to))
        return
    if args.raw:
        ns = argparse.Namespace(filter=args.filter, sort=None, size=args.size,
                                fields=args.fields, time_field=args.time_field,
                                time_from=args.time_from, time_to=args.time_to)
        print(json.dumps(opensearch_search(_build_body(args.index, args.query, ns), env)))
        return
    for h in opensearch_hits(args.index, args.query, env, filter=args.filter,
                             sort=args.sort, size=args.size, fields=args.fields,
                             time_field=args.time_field, time_from=args.time_from,
                             time_to=args.time_to):
        print(json.dumps(h, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

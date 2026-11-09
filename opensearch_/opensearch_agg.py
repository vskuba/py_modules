"""
Агрегации поверх opensearch_-сессии: terms-разбиения и date_histogram.

Точка входа общая, `python -m opensearch_.opensearch_ agg …`; здесь только
строитель тела запроса и расплющиватель бакетов. Транспорт, cookie-ротация и
смерть сессии — в `opensearch_.py`, не дублировать.

## Грабли (проверено на живом cashier-prod-fpm-*, OSD 3.9)

* **Поля — text с подполем `.keyword`** (`severity`, `jsonPayload.serviceName`, …).
  terms по голому text кластер отказывает: «Text fields are not optimised…» —
  прилетает как HTTP 400 через OSD и путается со сломанным запросом. Здесь это
  снимается само: при таком отказе запрос повторяется с `.keyword` на всех полях
  группировки. Поле даты (timestamp) трогать нельзя — `.keyword` у него нет.
* **Время — `timestamp`**, не `@timestamp` (см. docstring opensearch_).
* **terms отсекает по `--top`**: счётчик бакета — это внутри ветки, а не по
  окну целиком; «остальные» бакеты молча не попадают. Для полноты картины —
  параллельно `count`.
* Ключи бакетов в rawResponse — служебные `_g0`, `_g1`, `_hist` (поля-ключи
  ломались бы на точках в имени поля); настоящие поля видит `opensearch_agg_rows`.
"""

from __future__ import annotations

from opensearch_.opensearch_ import opensearch_filters, opensearch_search

KEYWORD_RETRY_MARK = "not optimised"


def opensearch_agg_run(index: str, groups=(), hist: str | None = None, interval: str = "1h",
                       query: str = "all", filters=(), time_from: str | None = None,
                       time_to: str | None = None, time_field: str = "timestamp",
                       top: int = 20, env: dict | None = None, raw: bool = False) -> list | dict:
    """Выполняет terms-цепочку (вложенно, по top на уровень) и/или date_histogram.

    Возвращает строки [(doc_count, [путь...])]; `raw=True` — сырой rawResponse
    целиком. При отказе «Text fields are not optimised» повторяет с `.keyword`
    на полях группировки.
    """
    body = _agg_body(groups, hist, interval, query, filters, time_from, time_to, time_field, top)
    try:
        res = opensearch_search({"params": {"index": index, "body": body}}, env)
    except SystemExit as e:
        if KEYWORD_RETRY_MARK not in str(e):
            raise
        kw = [g if g.endswith(".keyword") else g + ".keyword" for g in groups]
        if kw == list(groups):
            raise
        body = _agg_body(kw, hist, interval, query, filters, time_from, time_to, time_field, top)
        res = opensearch_search({"params": {"index": index, "body": body}}, env)
    return res if raw else opensearch_agg_rows(res, groups, hist)


def opensearch_agg_rows(res: dict, groups=(), hist: str | None = None) -> list:
    """Расплющивает бакеты rawResponse в строки [(doc_count, [путь...])]."""
    rows: list = []
    top_key = "_hist" if hist else ("_g0" if groups else None)
    if top_key is None:
        return rows
    _walk(res["aggregations"][top_key]["buckets"], -1 if hist else 0, [], rows)
    return rows


def _walk(buckets: list, depth: int, path: list, rows: list) -> None:
    for b in buckets:
        p = path + [b.get("key_as_string") or b.get("key", "?")]
        # OpenSearch кладёт вложенный agg прямо в бакет по имени; на других
        # кластерах тот же узел живёт под "aggregations" — берём где есть
        nxt = b.get("_g%d" % (depth + 1)) or b.get("aggregations", {}).get("_g%d" % (depth + 1))
        if nxt:
            _walk(nxt["buckets"], depth + 1, p, rows)
        else:
            rows.append((b["doc_count"], p))


def _agg_body(groups, hist, interval, query, filters, time_from, time_to, time_field, top) -> dict:
    body: dict = {"size": 0,
                  "query": {"bool": {"filter": opensearch_filters(query, filters,
                                                                  time_from, time_to, time_field)}}}
    aggs = _terms_chain(groups, top)
    if hist:
        hist_agg: dict = {"date_histogram": {"field": hist, "calendar_interval": interval,
                                            "min_doc_count": 1}}
        if aggs:
            hist_agg["aggs"] = {"_g0": aggs}
        body["aggs"] = {"_hist": hist_agg}
    elif aggs:
        body["aggs"] = {"_g0": aggs}
    return body


def _terms_chain(groups, top) -> dict | None:
    node = None
    for i in reversed(range(len(groups))):
        leaf: dict = {"terms": {"field": groups[i], "size": top}}
        if node is not None:
            leaf["aggs"] = {"_g%d" % (i + 1): node}
        node = leaf
    return node

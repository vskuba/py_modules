"""Грамматика и орфография текста через LanguageTool — и правка, не задевающая разметку.

Зачем: перевод с русского на украинский оставляет русизмы («вопрос»,
«без всякій», «кликни по»), которые глазами в длинной странице не ловятся,
а модель, если ей сказать «поправь язык», переписывает заодно и смысл, и
HTML. Здесь проверку делает словарь и правила LanguageTool (у украинского там
отдельная категория русизмов и кальок), а правку — точечная замена только в
тех местах, где лежит видимый текст.

```bash
python -m text_.text_grammar server                    # поднять LanguageTool (docker, :8010)
python -m text_.text_grammar check site/*.html --json out.json
python -m text_.text_grammar check --text "Кликни по кирилицу" --lang uk
python -m text_.text_grammar apply site/index.html --fixes fixes.json --dry-run
```

## Что считается текстом

Для `.html` — три вида мест, и только они:

- текстовые узлы вне `<style>`, `<code>`, `<pre>`, `<kbd>`, `<samp>`;
  строчные теги (`<b>`, `<a>`, `<span>`…) фразу не рвут — LanguageTool видит
  предложение целиком, блочные (`<p>`, `<td>`, `<li>`…) рвут;
- значения атрибутов `title`, `alt`, `aria-label`, `placeholder`, `content`
  (если в них есть кириллица) — это тоже видит человек;
- строковые литералы внутри `<script>`, где есть кириллица: подписи, которые
  скрипт вставляет в страницу. Комментарии JS пропускаются.

Остальные файлы (`.md`, `.txt`) проверяются целиком, по абзацам.

## Правка: `apply`

`fixes.json` — список `{"from": "...", "to": "...", "word": true}`. Замена
идёт **только внутри перечисленных выше мест**: тег, URL, имя класса, код JS
не трогаются, даже если в них то же слово. `word` (по умолчанию да) — границы
слова с учётом кириллицы и апострофа: `вопрос` не заденет `вопроси`.
Замена экранируется по месту: в литерале JS — его кавычка и `\\`
(прямой апостроф `зв'язує` в `'…'` иначе ломает весь скрипт — случилось
на первом же прогоне), в атрибуте — его кавычка, в тексте HTML — `< > &`.
Фраза, разорванная тегом (`без <b>всякій</b>`), одной заменой не найдётся —
делите на части. Каждая правка печатает число попаданий; ноль — выход с
кодом 1, чтобы молчаливый промах не прошёл.

## ⚠ Чего модуль НЕ делает

**Не решает, какая подсказка верна.** Первая подсказка LanguageTool для
русизма часто мимо (он предлагает ближайшее по буквам, а не по смыслу).
Выбор `to` — за тем, кто читает отчёт; модуль даёт отчёт и безопасную
замену.

**Не ловит всё.** Русизм, который сам является украинским словом с другим
значением, правилом не пойман. После `apply` перечитать — обязательно.

## Сервер

Локальный `erikvl87/languagetool` в docker, контейнер `languagetool`, порт
8010 (`LANGUAGETOOL_URL` переопределяет). Публичный `https://api.languagetool.org`
тоже подходит (`--url`), но там 20 запросов в минуту и 20 КБ на запрос, а
текст уходит наружу.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

LT_URL_DEFAULT = os.environ.get("LANGUAGETOOL_URL", "http://localhost:8010")
LT_IMAGE = "erikvl87/languagetool"
LT_CONTAINER = "languagetool"

# Чанк запроса: локальный сервер переварит и больше, но длинные запросы
# дольше ждут и хуже ретраятся; публичный API режет на 20 КБ.
CHUNK_CHARS = 15000

CYR = re.compile(r"[Ѐ-ӿ]")
SKIP_TAGS = {"style", "code", "pre", "kbd", "samp", "textarea"}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "button", "caption", "dd",
    "details", "div", "dl", "dt", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hr", "label", "li", "main", "nav",
    "ol", "option", "p", "section", "summary", "table", "tbody", "td", "text",
    "tfoot", "th", "thead", "title", "tr", "ul", "body", "head", "html", "svg", "g",
}
TEXT_ATTRS = {"title", "alt", "aria-label", "placeholder", "content"}
WORD_CH = r"[\wЀ-ӿ’'ʼ-]"

TOKEN = re.compile(
    r"<!--.*?-->"
    r"|<script\b[^>]*>(?P<js>.*?)</script\s*>"
    r"|<(?P<close>/?)(?P<tag>[a-zA-Z][\w:-]*)(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
    re.S,
)
ATTR = re.compile(r"""([\w:-]+)\s*=\s*("([^"]*)"|'([^']*)')""")


# --- сервер ---------------------------------------------------------------

def _lt_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v2/languages", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def text_grammar_server(url: str | None = None, wait_s: int = 120) -> str:
    """Поднять LanguageTool, если не отвечает: docker-контейнер `languagetool` на :8010; вернуть адрес."""
    url = url or LT_URL_DEFAULT
    if _lt_alive(url):
        return url
    if not url.startswith("http://localhost") and not url.startswith("http://127.0.0.1"):
        raise RuntimeError(f"LanguageTool по адресу {url} не отвечает")
    port = urllib.parse.urlparse(url).port or 8010
    existing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{LT_CONTAINER}$", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.strip()
    if existing:
        subprocess.run(["docker", "start", LT_CONTAINER], check=True, capture_output=True)
    else:
        subprocess.run(
            ["docker", "run", "-d", "--name", LT_CONTAINER, "--restart", "unless-stopped",
             "-p", f"127.0.0.1:{port}:8010", LT_IMAGE],
            check=True, capture_output=True,
        )
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if _lt_alive(url):
            return url
        time.sleep(2)
    raise RuntimeError(f"LanguageTool не поднялся за {wait_s} с: docker logs {LT_CONTAINER}")


# --- проверка -------------------------------------------------------------

def _lt_request(text: str, lang: str, url: str, disabled: tuple[str, ...]) -> list[dict]:
    data = {"text": text, "language": lang}
    if disabled:
        data["disabledRules"] = ",".join(disabled)
    body = urllib.parse.urlencode(data).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url.rstrip("/") + "/v2/check", data=body)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["matches"]
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(15 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(3)
                continue
            raise
    return []


def text_grammar_check(text: str, lang: str = "uk", url: str | None = None,
                       disabled: tuple[str, ...] = ()) -> list[dict]:
    """Проверить текст LanguageTool-ом; вернуть находки: offset, length, word, message, rule, category, replacements."""
    url = url or LT_URL_DEFAULT
    out: list[dict] = []
    # Режем по абзацам, чтобы смещения оставались точными.
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + CHUNK_CHARS)
        if end < len(text):
            cut = text.rfind("\n\n", pos, end)
            end = cut + 2 if cut > pos else end
        chunk = text[pos:end]
        for m in _lt_request(chunk, lang, url, disabled):
            off = pos + m["offset"]
            out.append({
                "offset": off,
                "length": m["length"],
                "word": text[off:off + m["length"]],
                "message": m.get("message", ""),
                "rule": m.get("rule", {}).get("id", ""),
                "category": m.get("rule", {}).get("category", {}).get("id", ""),
                "replacements": [r["value"] for r in m.get("replacements", [])[:5]],
            })
        pos = end
    return out


# --- места с текстом в HTML -----------------------------------------------

def _js_literals(src: str, base: int) -> list[tuple[int, int, str]]:
    """Смещения (start, end, кавычка) содержимого строковых литералов JS; комментарии пропускаются."""
    spans, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif c != "`" and src[j] == "\n":
                    break
                j += 1
            spans.append((base + i + 1, base + j, c))
            i = j + 1
        else:
            i += 1
    return spans


def text_grammar_regions(raw: str) -> list[dict]:
    """Места видимого текста в HTML: [{kind: text|attr|js, start, end, block}] — смещения в сыром файле."""
    regions: list[dict] = []
    skip = 0
    block = 0
    pos = 0
    for m in TOKEN.finditer(raw):
        if m.start() > pos and not skip:
            regions.append({"kind": "text", "start": pos, "end": m.start(), "block": block})
        pos = m.end()
        if m.group("js") is not None:
            block += 1
            for s, e, q in _js_literals(m.group("js"), m.start("js")):
                if CYR.search(raw[s:e]):
                    block += 1
                    regions.append({"kind": "js", "start": s, "end": e, "block": block, "quote": q})
            block += 1
            continue
        tag = (m.group("tag") or "").lower()
        if not tag:
            continue  # комментарий
        if tag in SKIP_TAGS:
            skip = max(0, skip - 1) if m.group("close") else skip + (0 if m.group(0).endswith("/>") else 1)
        if tag in BLOCK_TAGS:
            block += 1
        if not m.group("close") and m.group("attrs"):
            for a in ATTR.finditer(m.group("attrs")):
                if a.group(1).lower() in TEXT_ATTRS:
                    g = 3 if a.group(3) is not None else 4
                    s = m.start("attrs") + a.start(g)
                    e = m.start("attrs") + a.end(g)
                    if CYR.search(raw[s:e]):
                        block += 1
                        regions.append({"kind": "attr", "start": s, "end": e, "block": block,
                                        "quote": '"' if g == 3 else "'"})
                        block += 1
    if pos < len(raw) and not skip:
        regions.append({"kind": "text", "start": pos, "end": len(raw), "block": block})
    return regions


def _visible(raw_piece: str, kind: str) -> str:
    if kind == "js":
        raw_piece = re.sub(r"<[^>]+>", "", raw_piece)
        raw_piece = re.sub(r"\$\{[^}]*\}", " ", raw_piece)
        raw_piece = raw_piece.replace("\\n", " ").replace("\\'", "'").replace('\\"', '"')
    return html_mod.unescape(raw_piece)


def _blocks(raw: str) -> list[dict]:
    """Склеить места в блоки-фразы: [{text, line, kind}]."""
    out: list[dict] = []
    cur = None
    for r in text_grammar_regions(raw):
        piece = re.sub(r"\s+", " ", _visible(raw[r["start"]:r["end"]], r["kind"]))
        if cur is None or cur["block"] != r["block"]:
            cur = {"block": r["block"], "text": "", "line": raw.count("\n", 0, r["start"]) + 1,
                   "kind": r["kind"]}
            out.append(cur)
        cur["text"] += piece
    return [b for b in out if CYR.search(b["text"])]


def text_grammar_file(path: str, lang: str = "uk", url: str | None = None,
                      disabled: tuple[str, ...] = ()) -> list[dict]:
    """Проверить файл (.html — только видимый текст); вернуть находки с line, kind и context."""
    raw = open(path, encoding="utf-8").read()
    if path.endswith((".html", ".htm")):
        blocks = _blocks(raw)
    else:
        blocks, line = [], 1
        for para in re.split(r"\n\s*\n", raw):
            if CYR.search(para):
                blocks.append({"text": re.sub(r"\s+", " ", para), "line": line, "kind": "text"})
            line += para.count("\n") + 2
    joined, starts, p = [], [], 0
    for b in blocks:
        t = b["text"].strip()
        starts.append(p)
        joined.append(t)
        p += len(t) + 2
    text = "\n\n".join(joined)
    found = text_grammar_check(text, lang, url, disabled)
    import bisect
    for f in found:
        i = bisect.bisect_right(starts, f["offset"]) - 1
        b, off = blocks[i], f["offset"] - starts[i]
        t = joined[i]
        f.update(file=path, line=b["line"], kind=b["kind"],
                 context=t[max(0, off - 50):off] + "⟦" + f["word"] + "⟧" + t[off + f["length"]:off + f["length"] + 50])
    return found


# --- правка ---------------------------------------------------------------

def _escape_for(region: dict, to: str) -> str:
    """Замена должна остаться тем же видом текста: кавычка литерала JS или атрибута её не закрывает."""
    kind, q = region.get("kind"), region.get("quote")
    if kind == "js":
        to = to.replace("\\", "\\\\").replace(q, "\\" + q)
        return to.replace("${", "\\${") if q == "`" else to
    if kind == "attr":
        return to.replace("&", "&amp;").replace("<", "&lt;").replace(q, "&quot;" if q == '"' else "&#39;")
    return html_mod.escape(to, quote=False) if path_is_html(region) else to


def path_is_html(region: dict) -> bool:
    return region.get("html", True)


def text_grammar_apply(path: str, fixes: list[dict], dry_run: bool = False) -> list[dict]:
    """Заменить from→to только в видимом тексте HTML (узлы, атрибуты, литералы JS); вернуть число попаданий на каждую правку."""
    raw = open(path, encoding="utf-8").read()
    regions = text_grammar_regions(raw) if path.endswith((".html", ".htm")) else \
        [{"kind": "text", "start": 0, "end": len(raw), "html": False}]
    pats = []
    for f in fixes:
        variants = {f["from"], html_mod.escape(f["from"], quote=False)}
        body = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
        if f.get("word", True):
            body = rf"(?<!{WORD_CH})(?:{body})(?!{WORD_CH})"
        pats.append(re.compile(body))
    counts = [0] * len(fixes)
    parts, pos = [], 0
    for r in regions:
        parts.append(raw[pos:r["start"]])
        seg = raw[r["start"]:r["end"]]
        for k, (f, pat) in enumerate(zip(fixes, pats)):
            to = _escape_for(r, f["to"])
            seg, n = pat.subn(lambda _m, to=to: to, seg)
            counts[k] += n
        parts.append(seg)
        pos = r["end"]
    parts.append(raw[pos:])
    if not dry_run:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(parts))
    return [{"from": f["from"], "to": f["to"], "hits": c} for f, c in zip(fixes, counts)]


# --- CLI ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m text_.text_grammar", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("server", help="поднять LanguageTool и напечатать адрес")
    sv.add_argument("--url")
    ck = sub.add_parser("check", help="проверить файлы или --text")
    ck.add_argument("files", nargs="*")
    ck.add_argument("--text")
    ck.add_argument("--lang", default="uk")
    ck.add_argument("--url")
    ck.add_argument("--disable", action="append", default=[], help="id правила (повторяется)")
    ck.add_argument("--json", help="все находки — в файл JSON")
    apl = sub.add_parser("apply", help="применить правки from→to к файлу")
    apl.add_argument("file")
    apl.add_argument("--fixes", required=True, help="JSON: [{from, to, word?}]")
    apl.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "server":
        print(text_grammar_server(a.url))
        return 0
    if a.cmd == "apply":
        res = text_grammar_apply(a.file, json.load(open(a.fixes, encoding="utf-8")), a.dry_run)
        miss = 0
        for r in res:
            miss += r["hits"] == 0
            print(f"{r['hits']:>4}  {r['from']} → {r['to']}")
        print(f"{'(dry-run) ' if a.dry_run else ''}правок {len(res)}, без попаданий {miss}")
        return 1 if miss else 0

    url = text_grammar_server(a.url)
    found: list[dict] = []
    if a.text:
        found += [dict(f, file="<text>", line=1, kind="text",
                       context=a.text[max(0, f['offset'] - 50):f['offset']] + "⟦" + f["word"] + "⟧"
                       + a.text[f['offset'] + f['length']:f['offset'] + f['length'] + 50])
                  for f in text_grammar_check(a.text, a.lang, url, tuple(a.disable))]
    for p in a.files:
        found += text_grammar_file(p, a.lang, url, tuple(a.disable))
    for f in found:
        sug = ", ".join(f["replacements"][:3]) or "—"
        print(f"{f['file']}:{f['line']} [{f['kind']}] {f['rule']}: {f['context']}\n    {f['message']} → {sug}")
    by_rule: dict[str, int] = {}
    for f in found:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    print(f"\nнаходок {len(found)}: " + ", ".join(f"{k} {v}" for k, v in sorted(by_rule.items(), key=lambda x: -x[1])))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(found, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

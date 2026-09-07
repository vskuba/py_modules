"""
PDF: осмотр, рендер, сверка с эталоном, беление окон, печать HTML.

Модуль собран вокруг операций, из которых состоит форензика сгенерированного
PDF: какой размер страницы записан на самом деле (он не всегда A4!), какие
шрифты вложены, что текстом читается, а что запёкся в растр, и чем
отрендеренная копия отличается от эталонной.

Единицы: все координаты — пункты PDF (1/72 дюйма). MediaBox из `pdf_info` —
сырые координаты PDF (начало внизу слева); окна, которые передают в функции
беления и сверки, — наоборот, с началом в верхнем левом углу, перевод внутри
функций.
"""
import json
import os
import re

SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
# Расширения по формату вложенного потока — как их понимает font_.font_.
FONT_EXTENSIONS = {"TrueType": ".ttf", "OpenType": ".otf", "CFF": ".cff", "Type1": ".pfb"}


def pdf_info(path: str) -> dict:
    """
    Краткий паспорт документа: страницы, MediaBox (pt, постранично), шрифты,
    размер текстового слоя.

    `text_chars == 0` — текстового слоя нет, всё запёкся в растр;
    `subset` — шрифт является подмножеством (префикс `ABCDEF+`), сверять
    такое с мастером по контурам бессмысленно, см. `font_tooling.md`.
    """
    from pypdf import PdfReader

    reader = PdfReader(path)
    fonts: list[dict] = []
    seen: set[tuple] = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        font_dict = resources.get("/Font") if resources else None
        if not font_dict:
            continue
        for _key, ref in font_dict.items():
            obj = ref.get_object()
            base = str(obj.get("/BaseFont", "")).lstrip("/")
            subtype = str(obj.get("/Subtype", "")).lstrip("/")
            embedded = _font_stream(_descriptor(obj)) is not None
            marker = (base, subtype)
            if marker not in seen:
                seen.add(marker)
                fonts.append({
                    "basefont": base,
                    "subtype": subtype,
                    "embedded": bool(embedded),
                    "subset": bool(SUBSET_RE.match(base)),
                })

    text_chars = sum(len(page.extract_text() or "") for page in reader.pages)
    return {
        "version": reader.pdf_header.replace("%PDF-", ""),
        "pages": len(reader.pages),
        "size_bytes": os.path.getsize(path),
        "mediabox": [[float(v) for v in box] for box in
                     (page.mediabox for page in reader.pages)],
        "fonts": fonts,
        "text_chars": text_chars,
    }


def pdf_fonts_extract(path: str, out_dir: str | None = None) -> list[dict]:
    """
    Извлечь вложенные шрифты: `{"basefont","format","subset","bytes","file"}`.

    Байты доездают живыми до `font_.font_` (он принимает и путь, и байты) —
    так подмножество из PDF сверяют с мастер-файлом, не создавая временных.
    С `out_dir` каждый шрифт пишется файлом (`.ttf`/`.otf`/`.cff`/`.pfb`),
    в `file` — путь; без него `file` None. Форматы: TrueType (FontFile2),
    OpenType/CFF (FontFile3), Type1 (FontFile); невложенные шрифты
    (тот же Helvetica из набора) в список не попадают.
    """
    from pypdf import PdfReader

    reader = PdfReader(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)  # свежий каталог — норма CLI, руки не нужны
    out: list[dict] = []
    seen: set[tuple] = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        font_dict = resources.get("/Font") if resources else None
        if not font_dict:
            continue
        for _key, ref in font_dict.items():
            obj = ref.get_object()
            base = str(obj.get("/BaseFont", "")).lstrip("/")
            found = _font_stream(_descriptor(obj))
            if not found:
                continue
            stream, fmt = found
            marker = (base, fmt)
            if marker in seen:  # одна гарнитура живёт на каждой странице — не повторяться
                continue
            seen.add(marker)
            # Ключ есть, а потока нет (словарь без `stream`) — формальное
            # вложение с пустыми байтами: не выдумываем содержимое.
            data = stream.get_data() if hasattr(stream, "get_data") else b""
            item = {"basefont": base, "format": fmt,
                    "subset": bool(SUBSET_RE.match(base)), "bytes": data, "file": None}
            if out_dir:
                target = os.path.join(out_dir, base.replace("/", "_") + FONT_EXTENSIONS[fmt])
                with open(target, "wb") as fh:
                    fh.write(data)
                item["file"] = target
            out.append(item)
    return out


def pdf_text(path: str, page: int | None = None) -> str:
    """
    Текстовый слой документа (или одной страницы `page`). Пустая строка
    означает, что текст запёкся в растр.
    """
    from pypdf import PdfReader

    reader = PdfReader(path)
    if page is None:
        # Непустые страницы, склеенные переводом строки: две пустые страницы
        # дают "" , а не "\n" (проверено тестом).
        return "\n".join(t for t in (p.extract_text() or "" for p in reader.pages) if t)
    return reader.pages[page].extract_text() or ""


def pdf_render(path: str, out_dir: str, dpi: int = 150, page: int | None = None) -> list[str]:
    """
    Отрендерить страницы в PNG (`page-0.png`, …) и вернуть список файлов.

    Рендерер — pypdfium2: бинарное колесо, значит рендер одинаков на машине
    разработчика и в контейнере, без зависимости от poppler.
    """
    import pypdfium2 as pdfium

    out_paths = []
    # Документ закрывается всегда: незакрытый висит до сборки мусора, и на
    # пакетном прогоне pypdfium2 ругается «objects are still open».
    with pdfium.PdfDocument(path) as doc:
        os.makedirs(out_dir, exist_ok=True)  # вызывающий вправе указать свежий путь — CLI так и делает
        indices = range(len(doc)) if page is None else [page]
        for i in indices:
            bmp = doc[i].render(scale=dpi / 72.0)
            try:
                # Закрытие документа каскадом закрывает страницы, но не растры —
                # у растра свой буфер. И `to_pil()` кладёт картинку поверх этого
                # буфера, поэтому пиксели копируются (`convert`) до освобождения.
                img = bmp.to_pil().convert("RGB")
            finally:
                bmp.close()
            target = os.path.join(out_dir, f"page-{i}.png")
            img.save(target)
            out_paths.append(target)
    return out_paths


def pdf_diff(path_a: str, path_b: str, dpi: int = 150, out_dir: str | None = None,
             regions: list[dict] | None = None) -> dict:
    """
    Сравнить два PDF побиксельно в одинаковой плотности растра.

    Статистика — по всем общим страницам целиком (`max_diff`, `mean_abs`,
    `pct_pixels`); `regions` — список окон `{"name","x","y","w","h","page"?}`
    в пикселях данной плотности (нумерация страниц с нуля, окно по умолчанию
    на первой), по каждому своя статистика и поле `page` в ответе.
    `out_dir` — записать `side-by-side.png` (A | B | теплокарта разницы).

    `max_diff` до ~30 — копии совпадают в пределах растеризации;
    единичные 200+ на краях глифов текста — антиалиасинг движков, а не
    ошибка (см. `pdf_tooling.md`, §2).
    """
    import tempfile

    from PIL import Image, ImageChops

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        files_a = pdf_render(path_a, d1, dpi=dpi)
        files_b = pdf_render(path_b, d2, dpi=dpi)
        result = {
            "pages_a": len(files_a), "pages_b": len(files_b),
            "pages_match": len(files_a) == len(files_b),
        }
        if not files_a or not files_b:
            raise ValueError("один из документов отрендерился пустым")
        # Картинки читаются в память сразу: `Image.open` ленив и держит файл
        # открытым, а страниц столько же, сколько в документе, — на большом
        # PDF это упирается в лимит дескрипторов.
        imgs_a = [_image_load(f) for f in files_a]
        # Размеры могут разойтись на пиксели между движками — кандидат
        # приводится к сетке эталона.
        imgs_b = []
        for i in range(min(len(files_a), len(files_b))):
            ib = _image_load(files_b[i])
            if ib.size != imgs_a[i].size:
                ib = ib.resize(imgs_a[i].size)
            imgs_b.append(ib)
        n = min(len(imgs_a), len(imgs_b))
        combined = [_stats(imgs_a[i], imgs_b[i]) for i in range(n)]
        result["max_diff"] = max(c["max_diff"] for c in combined)
        result["mean_abs"] = round(sum(c["mean_abs"] for c in combined) / n, 3)
        result["pct_pixels"] = {
            "threshold": combined[0]["pct_pixels"]["threshold"],
            "pct": round(sum(c["pct_pixels"]["pct"] for c in combined) / n, 3),
        }

        if regions:
            result["regions"] = []
            for reg in regions:
                idx = reg.get("page", 0)
                if idx < 0 or idx >= min(len(imgs_a), len(imgs_b)):
                    raise ValueError(f"страница {idx} вне документа "
                                     f"(всего {min(len(imgs_a), len(imgs_b))})")
                box = (reg["x"], reg["y"], reg["x"] + reg["w"], reg["y"] + reg["h"])
                s = _stats(imgs_a[idx].crop(box), imgs_b[idx].crop(box))
                result["regions"].append({"name": reg.get("name", ""), "page": idx, **s})
        else:
            result["regions"] = []

        if out_dir:
            # A | B | теплокарта — три панели рядом, heat — L-канал разницы.
            w = imgs_a[0].width * 3
            strip = Image.new("RGB", (w, imgs_a[0].height), "white")
            strip.paste(imgs_a[0], (0, 0))
            strip.paste(imgs_b[0], (imgs_a[0].width, 0))
            heat = ImageChops.difference(imgs_a[0].convert("RGB"),
                                         imgs_b[0].convert("RGB")).convert("L").convert("RGB")
            strip.paste(heat, (imgs_a[0].width * 2, 0))
            target = os.path.join(out_dir, "side-by-side.png")
            strip.save(target)
            result["side_by_side"] = target
        else:
            result["side_by_side"] = None
    return result


def pdf_diff_multi(path_a: str, path_b: str, windows: dict, dpi: int = 150) -> dict:
    """
    Сверка документа с несколькими зонами за один проход.

    Обёртка над `pdf_diff`: документ рендерится один раз, а статистика
    считается и по всему листу, и по каждой зоне — удобно проверять, что
    правка не задела ничего вокруг.

    Args:
        path_a: эталон.
        path_b: кандидат.
        windows: `{'имя': (x, y, w, h)}` или `{'имя': (x, y, w, h, page)}`
            в пикселях `dpi`, координаты от верхнего левого угла страницы.
        dpi: плотность растра.

    Returns:
        `{'full': {'max_diff','mean_abs','pct_pixels','pages_match'},
        'windows': {'имя': {'max_diff','mean_abs','pct_pixels','page'}}}`.
    """
    regions = []
    for name, window in windows.items():
        if len(window) < 4:
            raise ValueError(f"окно '{name}': нужно (x, y, w, h[, page]), а дали {tuple(window)}")
        regions.append({"name": name, "x": window[0], "y": window[1],
                        "w": window[2], "h": window[3],
                        "page": window[4] if len(window) > 4 else 0})
    result = pdf_diff(path_a, path_b, dpi=dpi, regions=regions)
    full = {k: result[k] for k in ("max_diff", "mean_abs", "pct_pixels", "pages_match")}
    return {"full": full,
            "windows": {r["name"]: {k: v for k, v in r.items() if k != "name"}
                        for r in result["regions"]}}


def pdf_whiteout(path: str, out_png: str, boxes: list[dict], dpi: int = 300, page: int = 0) -> str:
    """
    Отрендерить страницу и выбелить `boxes` — каркас, на который сверху
    накладываются динамические данные.

    Координаты — пункты с началом в верхнем левом углу (HTML-стиль), в
    пиксели переводятся множителем `dpi / 72`. Плотность по умолчанию 300:
    каркас печатается фоновой картинкой, текст поверх него должен держать
    край.
    """
    import tempfile

    from PIL import Image, ImageDraw

    with tempfile.TemporaryDirectory() as d:
        src = pdf_render(path, d, dpi=dpi, page=page)[0]
        img = Image.open(src).convert("RGB")
        draw = ImageDraw.Draw(img)
        k = dpi / 72.0
        for b in boxes:
            draw.rectangle(
                [b["x"] * k, b["y"] * k, (b["x"] + b["w"]) * k, (b["y"] + b["h"]) * k],
                fill="white",
            )
        img.save(out_png)
    return out_png


def pdf_chrome() -> str | None:
    """Путь к бинарю headless-Chrome (первый найденный), None если нет."""
    import shutil

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def pdf_crop(src: str, box_mm, out_png: str, dpi: int = 150, page: int = 0,
             scale: float = 1.0) -> str:
    """
    Вырезать окно в миллиметрах из PDF-страницы или растрового файла и спасти PNG.

    Миллиметры — с началом сверху слева, как у `pdf_whiteout`: при сверке
    сгенерированного документа окна полей приезжают из раскладки именно в mm,
    и ручной перевод mm→px каждый раз — источник ошибки. Для растра (кадр
    снимка экрана, отрендеренная страница) `dpi` описывает сам файл — то же
    окно попадает в то же место макета. `scale` — увеличение для осмотра:
    край глифа виден на 3–4×.
    """
    from PIL import Image

    if src.lower().endswith(".pdf"):
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(src) as doc:
            bmp = doc[page].render(scale=dpi / 72.0)
            try:
                img = bmp.to_pil().convert("RGB")
            finally:
                bmp.close()
    else:
        img = Image.open(src).convert("RGB")
    k = dpi / 25.4
    img = img.crop(tuple(round(v * k) for v in box_mm))
    if scale != 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    img.save(out_png)
    return out_png


def _chrome_print(url: str, out_pdf: str | None, binary: str, profile: str,
                  vtb_ms: int, timeout_s: int):
    """Один запуск headless Chrome: адрес → PDF (или просто прогрев страницы).

    Флаги измерены на живой задаче: `--no-sandbox --disable-dev-shm-usage` —
    иначе дохнет в контейнере; отдельный `--user-data-dir` — несколько
    запусков не ловят «профиль заблокирован»; `--no-pdf-header-footer` —
    иначе штампуются дата и URL; `--virtual-time-budget` — ждать веб-шрифты
    и JS-разметку, иначе PDF выйдет шрифтом-заглушкой.
    """
    import subprocess

    cmd = [
        binary, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", f"--user-data-dir={profile}", "--no-pdf-header-footer",
        f"--virtual-time-budget={vtb_ms}",
    ]
    if out_pdf:
        cmd.append(f"--print-to-pdf={out_pdf}")
    return subprocess.run(cmd + [url], check=False, capture_output=True, timeout=timeout_s)


def _chrome_fail(out_pdf: str, proc) -> None:
    """Общий отказ печати: причина живёт в stderr движка, без хвоста провал — гадание."""
    if not os.path.exists(out_pdf) or os.path.getsize(out_pdf) < 1024:
        tail = proc.stderr.decode(errors="replace").strip()[-300:]
        raise RuntimeError(f"chrome не записал {out_pdf}" + (f": {tail}" if tail else ""))


def pdf_print_html(html_path: str, out_pdf: str, chrome: str | None = None, timeout_s: int = 30) -> None:
    """
    Напечатать HTML-страницу в PDF тем же движком (Skia), каким браузер
    печатает сам: документ векторный, размер страницы задаёт CSS `@page`.
    Странице, читающей `localStorage`, нужен `pdf_print_seeded` — на
    file:// хранилище другого происхождения недоступно.

    Бросает RuntimeError, если файл не появился (страница не отрендерилась).
    """
    import shutil
    import tempfile

    binary = chrome or pdf_chrome()
    if not binary or not os.path.exists(binary):
        raise RuntimeError("chrome не найден: поставьте google-chrome или передайте chrome=")
    profile = tempfile.mkdtemp(prefix="pdfprint-")
    try:
        proc = _chrome_print("file://" + os.path.abspath(html_path), out_pdf, binary,
                             profile, 15000, timeout_s)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    _chrome_fail(out_pdf, proc)


def pdf_seed_page(html_name: str, storage: dict) -> str:
    """
    Seed-страница: пишет `storage` в `localStorage` и переходит на `html_name`.

    Возвращает ASCII-строку: всё не-ASCII уходит в `\\uXXXX` — страницу,
    отдаваемую потоком без файла, легко снабдить неверным charset, и Chrome
    по эвристике выберет windows-1252, положив в хранилище mojibake
    (проверено на кириллических данных eVOD). Значения сериализуются
    `json.dumps`: dict/list приезжают как JSON, `str` хранится как есть
    (строка — законное значение localStorage). Скобки `<`/`>` в литералах
    экранируются: последовательность `</script>` закрыла бы блок внутри
    собственной страницы.
    """
    def _js(value) -> str:
        return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")

    lines = []
    for key, value in storage.items():
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"localStorage.setItem({_js(key)}, {_js(payload)});")
    return ('<!doctype html><meta charset="utf-8"><script>'
            + "\n".join(lines)
            + f"\nlocation.href = {_js(html_name)};"
            + "</script>")


def pdf_print_seeded(html_path: str, out_pdf: str, storage: dict,
                     chrome: str | None = None, timeout_s: int = 60) -> None:
    """
    Напечатать страницу в состоянии `storage` в `localStorage` страницы.

    Два прохода и http — необходимость, а не перестраховка: localStorage
    принадлежит origin, страница с `file://` его не видит вовсе. Каталог
    страницы раздаётся на 127.0.0.1 с эфемерным портом; первый запуск Chrome
    открывает seed-страницу (виртуальный обработчик, файлов в чужой каталог
    не пишем), та пишет хранилище и уходит на цель; второй запуск печатает
    цель. Оба запуска делят один временный профиль — localStorage живёт в
    нём, без общего профиля засев теряется.
    """
    import shutil
    import tempfile
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import quote

    binary = chrome or pdf_chrome()
    if not binary or not os.path.exists(binary):
        raise RuntimeError("chrome не найден: поставьте google-chrome или передайте chrome=")
    name = os.path.basename(os.path.abspath(html_path))
    seed = pdf_seed_page(name, storage)

    class _SeedHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/__pdf_seed__.html":
                raw = seed.encode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                super().do_GET()

        def log_message(self, *args):
            pass  # тишина: журнал нужен вызывающему, а не счёт запросов

    directory = os.path.dirname(os.path.abspath(html_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_SeedHandler, directory=directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    profile = tempfile.mkdtemp(prefix="pdfseed-")
    try:
        proc = _chrome_print(base + "__pdf_seed__.html", None, binary, profile, 8000, timeout_s)
        proc = _chrome_print(base + quote(name), out_pdf, binary, profile, 15000, timeout_s)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(profile, ignore_errors=True)
    _chrome_fail(out_pdf, proc)


def main() -> None:
    """CLI: `python -m pdf_.pdf_ info|text|render|diff|whiteout|print|extract|crop …`."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m pdf_.pdf_")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info"); p.add_argument("pdf")
    p = sub.add_parser("text"); p.add_argument("pdf"); p.add_argument("--page", type=int)
    p = sub.add_parser("render"); p.add_argument("pdf"); p.add_argument("out_dir"); p.add_argument("--dpi", type=int, default=150)
    p = sub.add_parser("diff"); p.add_argument("a"); p.add_argument("b"); p.add_argument("--dpi", type=int, default=150); p.add_argument("--out-dir")
    p = sub.add_parser("diff-multi"); p.add_argument("a"); p.add_argument("b"); p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--window", action="append", required=True, metavar="ИМЯ=x,y,w,h[,page]",
                   help="зона в пикселях --dpi, повторяется для нескольких зон")
    p = sub.add_parser("whiteout"); p.add_argument("pdf"); p.add_argument("out_png"); p.add_argument("--box", action="append", required=True, help="x,y,w,h в пунктах, начало сверху слева"); p.add_argument("--dpi", type=int, default=300)
    p = sub.add_parser("print"); p.add_argument("html"); p.add_argument("out_pdf")
    p.add_argument("--seed", action="append", metavar="КЛЮЧ=JSON",
                   help="значение localStorage КЛЮЧ (JSON или @файл); с --seed печать идёт http-раздачей в два прохода")
    p = sub.add_parser("extract"); p.add_argument("pdf"); p.add_argument("--out-dir")
    p = sub.add_parser("crop"); p.add_argument("src"); p.add_argument("box", help="x0,y0,x1,y1 в mm, начало сверху слева"); p.add_argument("out_png"); p.add_argument("--dpi", type=int, default=150); p.add_argument("--page", type=int, default=0); p.add_argument("--scale", type=float, default=1.0)

    a = parser.parse_args()
    if a.cmd == "info":
        print(json.dumps(pdf_info(a.pdf), ensure_ascii=False, indent=1))
    elif a.cmd == "text":
        sys.stdout.write(pdf_text(a.pdf, a.page))
    elif a.cmd == "render":
        print("\n".join(pdf_render(a.pdf, a.out_dir, dpi=a.dpi)))
    elif a.cmd == "diff":
        print(json.dumps(pdf_diff(a.a, a.b, dpi=a.dpi, out_dir=a.out_dir), ensure_ascii=False, indent=1))
    elif a.cmd == "diff-multi":
        windows = {}
        for spec in a.window:
            name, _, coords = spec.partition("=")
            windows[name] = tuple(float(v) if "." in v else int(v) for v in coords.split(","))
        print(json.dumps(pdf_diff_multi(a.a, a.b, windows, dpi=a.dpi), ensure_ascii=False, indent=1))
    elif a.cmd == "whiteout":
        boxes = []
        for s in a.box:
            x, y, w, h = (float(v) for v in s.split(","))
            boxes.append({"x": x, "y": y, "w": w, "h": h})
        print(pdf_whiteout(a.pdf, a.out_png, boxes, dpi=a.dpi))
    elif a.cmd == "print":
        if a.seed:
            # Разбор до запуска chrome: ошибка в JSON должна быть видна сразу,
            # а не после ожидания несостоявшегося рендера.
            storage = {}
            for spec in a.seed:
                key, sep, raw = spec.partition("=")
                if not sep:
                    raise SystemExit(f"--seed {spec!r}: ждём КЛЮЧ=JSON")
                if raw.startswith("@"):
                    raw = open(raw[1:], encoding="utf-8").read()
                try:
                    storage[key] = json.loads(raw)
                except ValueError as err:
                    raise SystemExit(f"--seed {key}: значение не JSON ({err})")
            pdf_print_seeded(a.html, a.out_pdf, storage)
        else:
            pdf_print_html(a.html, a.out_pdf)
        print(a.out_pdf)
    elif a.cmd == "extract":
        entries = pdf_fonts_extract(a.pdf, out_dir=a.out_dir)
        # Байты в JSON печатать бессмысленно: сводка с размером, файл уже на диске.
        print(json.dumps([{k: v for k, v in e.items() if k != "bytes"} | {"size": len(e["bytes"])}
                          for e in entries], ensure_ascii=False, indent=1))
    elif a.cmd == "crop":
        x0, y0, x1, y1 = (float(v) for v in a.box.split(","))
        print(pdf_crop(a.src, (x0, y0, x1, y1), a.out_png, dpi=a.dpi, page=a.page, scale=a.scale))



def _descriptor(font_obj):
    """FontDescriptor шрифта, разворачивая композитный Type0 к потомку.

    У Type0 дескриптор лежит у потомка (/DescendantFonts[0]), а не в самом
    объекте шрифта: без разворота вложенный шрифт всегда выглядел бы внешним.
    """
    desc = font_obj.get("/FontDescriptor")
    if desc is None and str(font_obj.get("/Subtype", "")) == "/Type0":
        kids = font_obj.get("/DescendantFonts") or []
        if kids:
            desc = kids[0].get_object().get("/FontDescriptor")
    return desc.get_object() if desc is not None else None


def _font_stream(desc):
    """Поток вложенного шрифта и его формат; None, если шрифт не вложен.

    FontFile3 — ключ многорольный: по подтипу потока отличается чистый CFF
    от полноценного OpenType. Поток с нулевой длиной — всё ещё вложение
    (формальный признак по ключу), байты просто пустые.
    """
    for key, fmt in (("/FontFile2", "TrueType"), ("/FontFile3", None), ("/FontFile", "Type1")):
        if desc is not None and key in desc:
            stream = desc[key].get_object()
            if key == "/FontFile3":
                fmt = "OpenType" if str(stream.get("/Subtype", "")) == "/OpenType" else "CFF"
            return stream, fmt
    return None


def _image_load(path: str):
    """
    Прочитать картинку целиком и отпустить файл.

    `Image.open` возвращает ленивый объект, держащий дескриптор до первого
    обращения к пикселям; копия внутри `with` отвязывает картинку от файла,
    сохраняя режим и данные.
    """
    from PIL import Image

    with Image.open(path) as img:
        return img.copy()


def _stats(img_a, img_b) -> dict:
    """Пиксельная статистика разницы: максимум по каналам, среднее по L, доля пикселей выше порога."""
    from PIL import ImageChops

    diff = ImageChops.difference(img_a.convert("RGB"), img_b.convert("RGB"))
    max_diff = max(diff.getchannel(c).getextrema()[1] for c in ("R", "G", "B"))
    gray = diff.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    mean_abs = sum(v * n for v, n in enumerate(hist)) / total
    threshold = 24  # ниже — шум растеризации, не различие
    pct = 100.0 * sum(hist[threshold + 1:]) / total
    return {"max_diff": max_diff, "mean_abs": round(mean_abs, 3),
            "pct_pixels": {"threshold": threshold, "pct": round(pct, 3)}}

if __name__ == "__main__":
    main()

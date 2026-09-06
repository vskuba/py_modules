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

    doc = pdfium.PdfDocument(path)
    os.makedirs(out_dir, exist_ok=True)  # вызывающий вправе указать свежий путь — CLI так и делает
    indices = range(len(doc)) if page is None else [page]
    out_paths = []
    for i in indices:
        bmp = doc[i].render(scale=dpi / 72.0)
        img = bmp.to_pil()
        target = os.path.join(out_dir, f"page-{i}.png")
        img.convert("RGB").save(target)
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
        imgs_a = [Image.open(f) for f in files_a]
        # Размеры могут разойтись на пиксели между движками — кандидат
        # приводится к сетке эталона.
        imgs_b = []
        for i in range(min(len(files_a), len(files_b))):
            ib = Image.open(files_b[i])
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


def pdf_print_html(html_path: str, out_pdf: str, chrome: str | None = None, timeout_s: int = 30) -> None:
    """
    Напечатать HTML-страницу в PDF тем же движком (Skia), каким браузер
    печатает сам: документ векторный, размер страницы задаёт CSS `@page`.

    Набор флагов не случайный, он измерен на живой задаче:
    `--no-sandbox --disable-dev-shm-usage` — иначе дохнет в контейнере;
    отдельный `--user-data-dir` — несколько запусков не ловят «профиль
    заблокирован»; `--no-pdf-header-footer` — иначе штампуются дата и URL;
    `--virtual-time-budget` — ждать загрузки веб-шрифтов и JS-разметки,
    иначе PDF выйдет шрифтом-заглушкой.

    Бросает RuntimeError, если файл не появился (страница не отрендерилась).
    """
    import shutil
    import subprocess
    import tempfile

    binary = chrome or pdf_chrome()
    if not binary or not os.path.exists(binary):
        raise RuntimeError("chrome не найден: поставьте google-chrome или передайте chrome=")
    profile = tempfile.mkdtemp(prefix="pdfprint-")
    cmd = [
        binary, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", f"--user-data-dir={profile}", "--no-pdf-header-footer",
        "--virtual-time-budget=15000", f"--print-to-pdf={out_pdf}",
        "file://" + os.path.abspath(html_path),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout_s)
    shutil.rmtree(profile, ignore_errors=True)
    if not os.path.exists(out_pdf) or os.path.getsize(out_pdf) < 1024:
        # Причина отказа живёт в stderr движка: без его хвоста провал — гадание.
        tail = proc.stderr.decode(errors="replace").strip()[-300:]
        raise RuntimeError(f"chrome не записал {out_pdf}" + (f": {tail}" if tail else ""))


def main() -> None:
    """CLI: `python -m pdf_.pdf_ info|text|render|diff|whiteout|print|extract …`."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m pdf_.pdf_")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info"); p.add_argument("pdf")
    p = sub.add_parser("text"); p.add_argument("pdf"); p.add_argument("--page", type=int)
    p = sub.add_parser("render"); p.add_argument("pdf"); p.add_argument("out_dir"); p.add_argument("--dpi", type=int, default=150)
    p = sub.add_parser("diff"); p.add_argument("a"); p.add_argument("b"); p.add_argument("--dpi", type=int, default=150); p.add_argument("--out-dir")
    p = sub.add_parser("whiteout"); p.add_argument("pdf"); p.add_argument("out_png"); p.add_argument("--box", action="append", required=True, help="x,y,w,h в пунктах, начало сверху слева"); p.add_argument("--dpi", type=int, default=300)
    p = sub.add_parser("print"); p.add_argument("html"); p.add_argument("out_pdf")
    p = sub.add_parser("extract"); p.add_argument("pdf"); p.add_argument("--out-dir")

    a = parser.parse_args()
    if a.cmd == "info":
        print(json.dumps(pdf_info(a.pdf), ensure_ascii=False, indent=1))
    elif a.cmd == "text":
        sys.stdout.write(pdf_text(a.pdf, a.page))
    elif a.cmd == "render":
        print("\n".join(pdf_render(a.pdf, a.out_dir, dpi=a.dpi)))
    elif a.cmd == "diff":
        print(json.dumps(pdf_diff(a.a, a.b, dpi=a.dpi, out_dir=a.out_dir), ensure_ascii=False, indent=1))
    elif a.cmd == "whiteout":
        boxes = []
        for s in a.box:
            x, y, w, h = (float(v) for v in s.split(","))
            boxes.append({"x": x, "y": y, "w": w, "h": h})
        print(pdf_whiteout(a.pdf, a.out_png, boxes, dpi=a.dpi))
    elif a.cmd == "print":
        pdf_print_html(a.html, a.out_pdf)
        print(a.out_pdf)
    elif a.cmd == "extract":
        entries = pdf_fonts_extract(a.pdf, out_dir=a.out_dir)
        # Байты в JSON печатать бессмысленно: сводка с размером, файл уже на диске.
        print(json.dumps([{k: v for k, v in e.items() if k != "bytes"} | {"size": len(e["bytes"])}
                          for e in entries], ensure_ascii=False, indent=1))



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

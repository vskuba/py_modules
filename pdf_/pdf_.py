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
            desc = obj.get("/FontDescriptor")
            if desc is None and subtype == "Type0":
                # У композитного шрифта дескриптор лежит у потомка
                # (/DescendantFonts[0]), а не в самом объекте Type0.
                kids = obj.get("/DescendantFonts") or []
                if kids:
                    desc = kids[0].get_object().get("/FontDescriptor")
            desc = desc.get_object() if desc is not None else {}
            embedded = any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
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
    `pct_pixels`); `regions` — список окон на странице 1
    `{"name","x","y","w","h"}` в пикселях данной плотности, по каждому своя
    статистика. `out_dir` — записать `side-by-side.png` (A | B | теплокарта
    разницы).

    `max_diff` до ~30 — копии совпадают в пределах растеризации;
    единичные 200+ на краях глифов текста — антиалиасинг движков, а не
    ошибка (см. `pdf_tooling.md`, §2).
    """
    import tempfile

    from PIL import Image, ImageChops

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
                box = (reg["x"], reg["y"], reg["x"] + reg["w"], reg["y"] + reg["h"])
                s = _stats(imgs_a[0].crop(box), imgs_b[0].crop(box))
                result["regions"].append({"name": reg.get("name", ""), **s})
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

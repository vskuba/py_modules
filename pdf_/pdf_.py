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

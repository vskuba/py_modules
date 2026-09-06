# PDF — чем смотреть, чем сверять, чем печатать

> PDF смотрят структурно (`pdf_info`: MediaBox, шрифты, наличие текстового
> слоя) и пиксельно (`pdf_diff` против эталона). Генерируют печатью браузера
> (`pdf_print_html`) — тогда документ векторный и размер страницы задаёт CSS
> `@page`. Инструмент — `py_modules/pdf_`, CLI: `python -m pdf_.pdf_`;
> зависимости — `pip install -r py_modules/requirements-doc.txt` (импорт
> модуля пройдёт и без них, упадёт вызов функции).

## 1. Структура: смотреть сначала `pdf_info`

`pdf_info(path)` — паспорт: страницы, `mediabox` каждой (в пунктах, 1 pt =
1/72 дюйма), шрифты, `text_chars`. Порядок разбора чужого PDF:

1. **MediaBox** — это реальный размер страницы, а не «A4 по умолчанию».
   Документ, напечатанный браузером из `@page`, несёт тот размер, что
   записан в CSS, и он редко совпадает с A4 до сотых.
2. **`text_chars == 0`** — текстового слоя нет, всё запечено в растр:
   такой документ нельзя ни проверить извлечением текста, ни скопировать
   из него значение.
3. **Шрифты**: `subset` (префикс `ABCDEF+`) — в файле подмножество,
   перекодируемое движком печати (CFF→glyf, масштаб upem). Сверять такое
   с мастер-шрифтом по контурам бесполезно — см. `font_tooling.md`.

Начало координат разное, и это частая ошибка:

| Что | Начало координат |
|-----|------------------|
| `mediabox` из `pdf_info` | PDF: снизу слева |
| окна `pdf_whiteout` / `pdf_diff` | HTML-стиль: сверху слева |

## 2. Мост к шрифтам: `pdf_fonts_extract`

`pdf_fonts_extract(pdf)` отдаёт вложенные шрифты байтами
(`{"basefont","format","subset","bytes","file"}`), с `--out-dir` ещё и пишет
файлами. Байты — прямой вход в `font_` (его функции принимают путь, байты и
поток), временные файлы не нужны:

```python
from pdf_.pdf_ import pdf_fonts_extract
from font_.font_ import font_compare

subset = pdf_fonts_extract("card.pdf")[0]
font_compare(subset["bytes"], "master.ttf")   # сверка, нормированная по upem
```

CLI: `python -m pdf_.pdf_ extract card.pdf --out-dir /tmp/fonts`.

## 3. Сверка с эталоном: `pdf_render` + `pdf_diff`

Пиксельное сравнение — рендер обоих файлов в ту же плотность и статистика
разницы. Порог «это разница или нет»:

- `max_diff` до ~30 — копии совпадают в пределах растеризации;
- одиночные 200+ **на краях глифов текста** — антиалиасинг разных движков,
  а не ошибка; смотреть надо области, а не пиксели;
- систематический сдвиг всего содержимого — ищи округление макета: Chrome
  выставляет растровые боксы в целые CSS-пиксели (см. §4), компенсация —
  `transform: scale(...)` с коэффициентом «точные pt / целые px».

Плотность для текстовых документов — 300 dpi: ниже — не видно расхождений
кернинга, выше — шум сжатия. Окна проверок (`regions`) задаются в пикселях
этой же плотности; ключ `"page"` (нумерация с нуля, по умолчанию первая)
переносит окно на любую страницу — страница в ответе эхом возвращается в
каждом окне.

## 4. Генерация: печатать браузером, не рисовать руками

`pdf_print_html(html, out)` — тот же движок (Skia), которым браузер сам
печатает страницы: векторный PDF, `@page size … margin: 0` задаёт MediaBox
точно. Флаги в функции не случайны: без отдельного профиля и
`--virtual-time-budget` веб-шрифт не успевает загрузиться и PDF выходит
шрифтом-заглушкой; без `--no-pdf-header-footer` лезут дата и URL.

Растровый каркас (выбеленный PNG страницы, на него накладываются данные)
вёрстается с компенсацией: картинку ставить в **целые** CSS-пиксели и
возвращать точные размеры `transform: scale(...)` — трансформации красятся
дробно, а макет — округляется.

## 5. Минимальный PDF для теста — писать руками

Тесту не нужен настоящий генератор: документ собирается из тел объектов, а
смещения xref считаются при сборке. Тело — строка (словари) или байты (поток
шрифта бинарный, строкой его не выразить):

```python
def raw_pdf(path, objects) -> str:
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        body_bytes = body if isinstance(body, bytes) else body.encode()
        out += f"{i} 0 obj\n".encode() + body_bytes + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    with open(path, "wb") as fh:
        fh.write(out)
    return str(path)
```

**Хвост из xref, трейлера и `%%EOF` — не формальность**, ради него функция и
существует. Файл без него pypdf отвергает, но исключение показывает не туда:
`PdfStreamError: Stream has ended unexpectedly` — про конец потока, тогда как
дело в отсутствующей таблице. Настоящая подсказка приходит строкой выше и
отдельно от исключения — предупреждением `EOF marker not found`.

Объекты нумеруются с единицы в порядке списка, `/Root` — всегда `1 0 R`, ссылки
внутри тел проставляются вручную. Двухстраничный документ с текстом и
композитным Type0, у которого дескриптор лежит у потомка (§1):

```python
content = b"BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET"
raw_pdf(path, [
    "<</Type/Catalog/Pages 2 0 R>>",
    "<</Type/Pages/Kids[3 0 R 8 0 R]/Count 2>>",
    "<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R"
    "/F2 5 0 R>>>>/Contents 7 0 R>>",
    "<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>",
    "<</Type/Font/Subtype/Type0/BaseFont/ABCDEF+TestFont/Encoding/Identity-H"
    "/DescendantFonts[6 0 R]>>",
    "<</Type/Font/Subtype/CIDFontType2/BaseFont/ABCDEF+TestFont/CIDSystemInfo"
    "<</Registry(Adobe)/Ordering(Identity)/Supplement 0>>/FontDescriptor 9 0 R>>",
    f"<</Length {len(content)}>>\nstream\n{content.decode()}\nendstream",
    "<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Resources<<>>>>",
    "<</Type/FontDescriptor/FontName/ABCDEF+TestFont/Flags 4/FontFile2 <</Length 0>>>>",
])
```

Растровый образец (без текстового слоя) проще собрать Pillow: `img.save(path,
save_all=True, append_images=[...])` — этого хватает на MediaBox, страницы и
проверку `text_chars == 0`.

## 6. Чек-лист принятия сгенерированного PDF

1. `pdf_info` кандидата: страницы, MediaBox — как у эталона; `text_chars > 0`
   там, где обещан текстовый слой.
2. `pdf_diff(эталон, кандидат, dpi=300, out_dir=…)` — статистика +
   side-by-side глазами на текстовые блоки.
3. Шрифт — не заглушка (`pdf_info.fonts[].basefont`).
4. CLI: `python -m pdf_.pdf_ diff gold.pdf cand.pdf --dpi 300 --out-dir /tmp/d`.

## Грабли

1. **MediaBox ≠ A4** — размер страницы может быть 595.92×841.92 pt; сверять
   надо с фактом эталона, а не с «стандартным листом».
2. **Chrome округляет растровые боксы до целых CSS-пикселей** — фон-картинка
   растягивается на доли пикселя, текст «уезжает»; лечится целым боксом +
   `transform: scale(...)`.
3. **HTML-текст не ставится по базовой линии** — позиция строки в движке
   печати не детерминирована; точная типографика — SVG `<text>` с `y`
   базовой линии.
4. **Веб-шрифт при печати без `--virtual-time-budget` не догружается** —
   PDF выглядит «похоже, но не то»; проверять `pdf_info.fonts`.
5. **Подмножество шрифта в PDF — не мастер** — его контуры перекодированы;
   сверка «мастер vs файл» по глифам лжёт, см. `font_tooling.md`.
6. **`FontFile2` — не всегда поток** — встречается словарь без `stream`
   (формальное вложение): признак на месте, байты пустые; `pdf_fonts_extract`
   на таких отдаёт `b""`, а не падает.

## Что читать рядом

| Вопрос | Файл |
|--------|------|
| Шрифты: паспорт, мастер vs подмножество | `font_tooling.md` |
| Телефон, снимки экрана | `mobile_device.md` |

"""
PDF-отчёт из папки с артефактами: печать, превью страниц, contact sheet, проверки вёрстки.

Когда отчёт собирают руками из артефактов stage-прогона (HTML или Markdown со
ссылками на картинки), вёрстка ломается в двух местах, и оба видны только после
печати: картинка выше высоты тела страницы (в A4 landscape тело ~132 mm, а
`img {max-height:172mm}` из портретного шаблона — фигура не влезает, остаток
секции уезжает на пустую страницу) и пустая страница после неё. Здесь оба
грабля названы прямо: при печати max-height фигур дописывается поверх
авторского CSS, готовый PDF проверяется — чернила на странице (`pdf_render` +
доля тёмных пикселей) и прямоугольники картинок (`pdf_xobject_get`).

Источники (в `src` — каталоге или файле): `report.html` (авторский HTML
сохраняется как есть, к нему только дописывается исполнительский CSS),
`report.md` (заголовки, абзацы, списки, таблицы `|…|`, фигуры `![подпись](путь)`),
`sections.json` (`{'title','meta':{…}, 'sections':[{title,text,figure:{src,caption}}]}`).
Наружу: PDF, постраничные PNG-превью, contact sheet, список проблем; проверки
не пройдены — CLI уходит с ненулевым кодом. Секреты — та же страховка, что у
карточки: `--forbid` гоняет `data_.data_mask.data_mask_sweep` по HTML и тексту PDF.

CLI:

    python -m pdf_.pdf_report build report-dir --out report.pdf --landscape
    python -m pdf_.pdf_report check report.pdf [--figure-mm 132] [--min-ink 8]
    python -m pdf_.pdf_report build report-dir --out report.pdf --forbid secrets.txt
"""
import argparse
import html as _html_mod
import json
import os
import re
import tempfile

from data_.data_mask import data_mask_sweep, data_mask_values

PDF_REPORT_INK_MIN_PCT = 8.0  # страница с чернилами ниже — почти пустая (orphan)
PDF_REPORT_INK_DPI = 80       # плотность, на которой считается чернильность
PDF_REPORT_INK_CUTOFF = 241   # пиксель темнее 240 по L — чернила, не бумага
PDF_REPORT_PREVIEW_DPI = 150  # постраничные превью
PDF_REPORT_COVER_MIN = 10.0   # картинок на 10 %+ площади — страница уже не пуста
_MM = 25.4 / 72               # пунктах страницы в миллиметры
# Потолок высоты фигуры: A4 landscape c полем 11 mm — тело ~132 mm (измерено
# на отчёте 11.27); портрет — 160 mm, остальное занимает заголовок и текст.
PDF_REPORT_FIGURE_MM = {'landscape': 132, 'portrait': 160}


def pdf_report_build(src: str, out_pdf: str, landscape: bool = False,
                     figure_mm: float | None = None, min_ink: float = PDF_REPORT_INK_MIN_PCT,
                     skip_pages=(), forbid=(), print_fn=None, chrome: str | None = None) -> dict:
    """Собрать PDF-отчёт из `src` (каталог с report.html/report.md/sections.json или файл); вернуть сводку.

    Печать — `pdf_print_html` (Skia: вектор, `@page` задаёт CSS), печать
    подменяется `print_fn(html_path, out_pdf)` — тесты живут без chrome.
    Проверки после печати: `pdf_report_check` + sweep запрещённых значений по
    собранному HTML, тексту PDF и дереву `src`.

    Args:
        src: каталог с источником отчёта (или сам файл источника).
        out_pdf: куда лечь PDF; превью и лист кладутся в `<out_pdf>-out/`.
        landscape: альбомная A4 (`@page size:A4 landscape`).
        figure_mm: потолок высоты фигуры; по умолчанию по ориентации.
        min_ink: процент чернил, ниже которого страница считается пустой
            (если только на ней нет фигуры, занимающей 10 %+ площади).
        skip_pages: номера страниц (с 1), которые не проверять на пустоту.
        forbid: значения, которым нельзя попасть в артефакты.
        print_fn: `fn(html_path, out_pdf)`; по умолчанию `pdf_print_html`.
        chrome: явный бинарь chrome для печати.

    Returns:
        {'pdf','html','pages','sheet','issues','sweep','ok'}; `ok` — вранье
        не найдено ни в вёрстке, ни в значениях.
    """
    cap = figure_mm or PDF_REPORT_FIGURE_MM['landscape' if landscape else 'portrait']
    html_path = _assemble_html(src, landscape, cap)
    do_print = print_fn or (lambda h, p: _print_default(h, p, chrome))
    out_pdf = os.fspath(out_pdf)
    work = os.path.splitext(out_pdf)[0] + '-out'
    os.makedirs(work, exist_ok=True)
    do_print(html_path, out_pdf)
    from pdf_.pdf_ import pdf_render
    from pdf_.pdf_sheet import pdf_contact_sheet
    pages = pdf_render(out_pdf, work, dpi=PDF_REPORT_PREVIEW_DPI)
    sheet = pdf_contact_sheet(out_pdf, os.path.join(work, 'contact-sheet.png'))['out']
    issues = pdf_report_check(out_pdf, figure_mm=cap, min_ink=min_ink, skip_pages=skip_pages)
    swept = None
    if forbid:
        values, err = data_mask_values(forbid) if isinstance(forbid, str) else (list(forbid), None)
        if err:
            raise RuntimeError(f'forbid: {err}')
        targets = ([src] if os.path.isdir(src) else [src]) + [html_path, out_pdf]
        swept, err = data_mask_sweep(targets, values)
        if err:
            raise RuntimeError(f'forbid: {err}')
    return {'pdf': out_pdf, 'html': html_path, 'pages': pages, 'sheet': sheet,
            'issues': issues, 'sweep': swept,
            'ok': not issues and not (swept and swept['hits'])}


def pdf_report_check(pdf_path: str, figure_mm: float | None = None,
                     min_ink: float = PDF_REPORT_INK_MIN_PCT, skip_pages=()) -> list:
    """Проверить готовый PDF на вранье вёрстки; вернуть список проблем (пусто — чисто).

    Две проверки: доля тёмных пикселей на растере страницы (`ink_pct`) —
    страница ниже `min_ink` **и без картинок** это тот самый orphan после
    невылезшей фигуры; картинка, занятая 10 %+ площади, — уже содержимое:
    скриншоты живых страниц белые и по чернилам не читаются. И высота каждого
    нарисованного Image-XObject — выше потолка фигуры значит картинка не
    влезает в тело и толкает за собой остаток секции. `figure_mm` по умолчанию
    берётся по ориентации первой страницы.
    """
    from PIL import Image

    from pdf_.pdf_ import pdf_info, pdf_render
    from pdf_.pdf_xobject import pdf_xobject_get

    info = pdf_info(pdf_path)
    box = info['mediabox'][0]
    cap = figure_mm or (PDF_REPORT_FIGURE_MM['landscape'] if box[2] > box[3]
                        else PDF_REPORT_FIGURE_MM['portrait'])
    issues = []
    with tempfile.TemporaryDirectory(prefix='pdfreport-') as tmp:
        rasters = pdf_render(pdf_path, tmp, dpi=PDF_REPORT_INK_DPI)
        for number, raster in enumerate(rasters, start=1):
            page = info['mediabox'][min(number - 1, len(info['mediabox']) - 1)]
            try:
                xobjects = pdf_xobject_get(pdf_path, page=number - 1)
            except Exception as exc:  # разбор потока чужого PDF может не удаться
                xobjects = None
                issues.append({'page': number, 'kind': 'unreadable',
                               'detail': f'потоки страницы не разобраны: {exc}'})
            if number in skip_pages:
                continue
            img = Image.open(raster).convert('L')
            hist = img.histogram()
            ink = 100.0 * sum(hist[:PDF_REPORT_INK_CUTOFF]) / max(1, img.width * img.height)
            cover = 0.0
            for xo in xobjects or ():  # clipping не вернём — считаем площадь прямоугольника
                x, y, w, h = xo['rect_mm']
                clipped = (max(0.0, min(x + w, page[2] * _MM) - max(x, 0.0))
                           * max(0.0, min(y + h, page[3] * _MM) - max(y, 0.0)))
                cover += 100.0 * clipped / max(1.0, (page[2] - page[0]) * (page[3] - page[1]) * _MM ** 2)
            if ink < min_ink and cover < PDF_REPORT_COVER_MIN:
                issues.append({'page': number, 'kind': 'empty-page',
                               'ink_pct': round(ink, 1), 'cover_pct': round(cover, 1),
                               'detail': f'чернил {ink:.1f}% < {min_ink}%, картинок {cover:.0f}%'})
            for xo in xobjects or ():
                height_mm = xo['rect_mm'][3]
                if height_mm > cap + 0.5:
                    issues.append({'page': number, 'kind': 'tall-figure', 'name': xo['name'],
                                   'height_mm': round(height_mm, 1),
                                   'detail': f'фигура {height_mm:.0f} mm выше тела {cap} mm'})
    return issues


# ── приватное ──


def _print_default(html_path: str, out_pdf: str, chrome) -> None:
    from pdf_.pdf_ import pdf_print_html
    pdf_print_html(html_path, out_pdf, chrome=chrome)


def _assemble_html(src: str, landscape: bool, cap: float) -> str:
    """Собрать печатаемый HTML рядом с источником; вернуть его путь.

    Рядом — не прихоть: figure ссылаются на `png/…` относительно источника, и
    chrome печатает `file://` именно оттуда.
    """
    root = src if os.path.isdir(src) else os.path.dirname(os.path.abspath(src))
    for name in ('report.html', 'report.htm'):
        if os.path.isfile(os.path.join(root, name)):
            body = open(os.path.join(root, name), encoding='utf-8').read()
            return _write_sibling(root, name, _inject_css(body, cap))
    source = os.path.join(src, 'report.md') if os.path.isdir(src) else src
    if os.path.isfile(source) and source.endswith('.md'):
        text = open(source, encoding='utf-8').read()
        return _write_sibling(os.path.dirname(source) or '.', 'report.md',
                              _shell(landscape, cap, _md_html(text)))
    spec_path = os.path.join(src, 'sections.json') if os.path.isdir(src) else src
    if os.path.isfile(spec_path) and spec_path.endswith('.json'):
        spec = json.load(open(spec_path, encoding='utf-8'))
        return _write_sibling(os.path.dirname(spec_path) or '.', 'sections.json',
                              _shell(landscape, cap, _sections_html(spec)))
    raise FileNotFoundError(
        f'в {src} нет источника: ожидал report.html, report.md или sections.json')


def _write_sibling(root: str, source_name: str, html_text: str) -> str:
    path = os.path.join(root, os.path.splitext(source_name)[0] + '-print.html')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(html_text)
    return path


def _inject_css(body: str, cap: float) -> str:
    """Дописать исполнительский CSS ПОСЛЕ авторского: равная специфичность,
    последние правила выигрывают; `!important` — против inline-стилей."""
    style = ('<style>figure img,figure svg,img{max-width:100%!important;'
             f'max-height:{cap}mm!important}}figure{{page-break-inside:avoid;'
             'break-inside:avoid}}</style>')
    head = re.search(r'</head\s*>', body, flags=re.I)
    if head:
        return body[:head.start()] + style + body[head.start():]
    return style + body


def _shell(landscape: bool, cap: float, body: str) -> str:
    size = 'A4 landscape' if landscape else 'A4'
    css = f"""@page {{ size: {size}; margin: 11mm; }}
html {{ -webkit-print-color-adjust: exact; }}
body {{ font: 10.5pt/1.5 Arial, sans-serif; color: #111827; margin: 0; }}
h1 {{ font-size: 17pt; margin: 0 0 4pt; }} h2 {{ font-size: 12.5pt; margin: 8pt 0 3pt; }}
h3 {{ font-size: 11pt; margin: 6pt 0 2pt; }}
p {{ margin: 3pt 0; }} code {{ font-family: ui-monospace, Menlo, monospace; background: #f1f5f9; }}
table {{ border-collapse: collapse; margin: 4pt 0; }}
td, th {{ border: 1px solid #cbd5e1; padding: 2px 6px; font-size: 9.5pt; text-align: left; }}
figure {{ page-break-inside: avoid; break-inside: avoid; margin: 5pt 0; }}
img {{ max-width: 100%; max-height: {cap}mm; }}
figcaption {{ font-size: 9pt; color: #475569; }}
.sec {{ page-break-before: always; }} .sec:first-of-type {{ page-break-before: auto; }}
"""
    return (f'<!doctype html><html><head><meta charset=utf-8><style>{css}</style>'
            f'</head><body>{body}</body></html>')


_MD_HEADING = re.compile(r'^(#{1,4})\s+(.*)$')
_MD_FIGURE = re.compile(r'^!\[([^\]]*)\]\(([^)]+)\)$')


def _md_html(text: str) -> str:
    """Markdown → HTML subset: заголовки, абзацы, списки, таблицы, фигуры `![c](src)`."""
    out, para, items, rows = [], [], [], []

    def flush():
        if rows:
            # `-` в конце класса: в середине `:-|` превращается в диапазон `:`–`|`
            cells = [[_inline(c.strip()) for c in r.strip().strip('|').split('|')]
                     for r in rows if not re.match(r'^\|?[\s|:-]+\|?$', r)]
            head = f'<tr><th>{"</th><th>".join(cells[0])}</th></tr>' if cells else ''
            out.append(f'<table>{head}' +
                       ''.join(f'<tr><td>{"</td><td>".join(c)}</td></tr>' for c in cells[1:]) +
                       '</table>')
            rows.clear()
        if items:
            out.append('<ul>' + ''.join(f'<li>{_inline(i)}</li>' for i in items) + '</ul>')
            items.clear()
        if para:
            out.append('<p>' + _inline(' '.join(para)) + '</p>')
            para.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith('|'):
            if para or items:
                flush()
            rows.append(stripped)
            continue
        heading = _MD_HEADING.match(stripped)
        figure = _MD_FIGURE.match(stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f'<h{level}>{_inline(heading.group(2))}</h{level}>')
        elif figure:
            flush()
            caption = f'<figcaption>{_inline(figure.group(1))}</figcaption>' if figure.group(1) else ''
            out.append(f'<figure><img src="{figure.group(2)}" alt="">{caption}</figure>')
        elif stripped.startswith(('- ', '* ')):
            if para:
                flush()
            items.append(stripped[2:])
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return ''.join(out)


def _sections_html(spec: dict) -> str:
    """sections.json → HTML: заголовок, meta-таблица, секции с текстом и фигурой."""
    parts = [f'<h1>{_inline(spec.get("title", ""))}</h1>']
    meta = spec.get('meta')
    if isinstance(meta, dict) and meta:
        parts.append('<table>' + ''.join(
            f'<tr><th>{_inline(k)}</th><td>{_inline(str(v))}</td></tr>'
            for k, v in meta.items()) + '</table>')
    elif meta:
        parts.append(f'<p>{_inline(str(meta))}</p>')
    for sec in spec.get('sections') or []:
        parts.append('<div class=sec>')
        if sec.get('title'):
            parts.append(f'<h2>{_inline(sec["title"])}</h2>')
        for chunk in re.split(r'\n\s*\n', str(sec.get('text', ''))):
            if chunk.strip():
                parts.append(f'<p>{_inline(chunk.strip())}</p>')
        figure = sec.get('figure') or {}
        if figure.get('src'):
            caption = f'<figcaption>{_inline(figure.get("caption", ""))}</figcaption>' \
                if figure.get('caption') else ''
            parts.append(f'<figure><img src="{figure["src"]}" alt="">{caption}</figure>')
        parts.append('</div>')
    return ''.join(parts)


def _inline(text) -> str:
    out = _html_mod.escape(str(text), quote=False)
    out = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', out)
    out = re.sub(r'`([^`]+)`', r'<code>\1</code>', out)
    out = re.sub(r'\[([^\]]+)\]\((https?[^)]*)\)', r'<a href="\2">\1</a>', out)
    return out


def _cli(argv=None):
    ap = argparse.ArgumentParser(prog='python -m pdf_.pdf_report',
                                 description='PDF-отчёт из папки: печать, превью, проверки вёрстки')
    ap.add_argument('cmd', choices=('build', 'check'))
    ap.add_argument('src', help='каталог с report.html/report.md/sections.json (build) или PDF (check)')
    ap.add_argument('--out', default='report.pdf', help='build: путь PDF')
    ap.add_argument('--landscape', action='store_true', help='build: альбомная A4')
    ap.add_argument('--figure-mm', type=float, default=None, metavar='MM',
                    help='потолок высоты фигуры; по умолчанию 132 (landscape) / 160')
    ap.add_argument('--min-ink', type=float, default=PDF_REPORT_INK_MIN_PCT, metavar='PCT',
                    help='доля чернил, ниже которой страница считается пустой')
    ap.add_argument('--skip-page', action='append', default=[], type=int,
                    help='страница (с 1), не проверять на пустоту; можно несколько')
    ap.add_argument('--forbid', default='', metavar='FILE',
                    help='список значений (JSON-массив или построчно), которым нельзя попасть в отчёт')
    ns = ap.parse_args(argv)
    forbid = ns.forbid
    if ns.cmd == 'build':
        got = pdf_report_build(ns.src, ns.out, landscape=ns.landscape, figure_mm=ns.figure_mm,
                               min_ink=ns.min_ink, skip_pages=ns.skip_page, forbid=forbid)
        print(f"PDF: {got['pdf']}\nпревью: {os.path.dirname(got['pages'][0]) if got['pages'] else '?'}"
              f"\nлист: {got['sheet']}")
    else:
        issues = pdf_report_check(ns.src, figure_mm=ns.figure_mm, min_ink=ns.min_ink,
                                  skip_pages=ns.skip_page)
        hits = []
        if forbid:
            values, err = data_mask_values(forbid)
            if err:
                raise SystemExit(f'ошибка: forbid: {err}')
            swept, err = data_mask_sweep([ns.src], values)
            if err:
                raise SystemExit(f'ошибка: forbid: {err}')
            hits = swept['hits']
        got = {'issues': issues, 'sweep': {'hits': hits},
               'ok': not issues and not hits}
    for issue in got['issues']:
        print(f"FAIL страница {issue['page']}: {issue['kind']} — {issue['detail']}")
    for hit in (got.get('sweep') or {}).get('hits', []):
        print(f"FAIL {hit['file']}: значение {hit['value']}… x{hit['count']}")
    print('PASS' if got['ok'] else 'FAIL: правь вёрстку — см. проблемы выше')
    raise SystemExit(0 if got['ok'] else 1)


if __name__ == '__main__':
    _cli()

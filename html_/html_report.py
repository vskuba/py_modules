"""
Скелет QA-отчёта для печати: обложка, легенда, оглавление, части и секции —
с CSS, подогнанным под A4-печать Chrome.

Половина времени сборки PDF-документа уходила не на печать, а на разметку:
`@page` с колонтитулами, переносы частей, оглавление точками — каждый
документ сочинялся заново и каждый раз всплывали те же грабли (колонки
`break-inside`, пустой колонтитул обложки). Здесь каркас одной функцией:
`html_report(spec)` возвращает HTML, в котором остаётся писать только тела
секций; `<span class="pg" data-for>` оглавления пустые — номера проставит
`pdf_.pdf_toc.pdf_build_toc` при печати.

Структура spec (строки — HTML-фрагменты, экранированием занимается автор):

    {'title': 'E2E: план покрытия', 'meta': '26.11 · Cashier · v2',
     'lead': '<p>Что внутри и зачем</p>', 'legend': '<table>…</table>',
     'parts': [{'id': 'p1', 'num': 'I', 'title': 'Текущее состояние',
                'sub': 'чем живём сейчас',
                'sections': [{'id': 's-num', 'num': '1', 'title': 'Цифры',
                              'body': '<p>…</p>', 'page': False}]}]}

⚠ `page: true` у секции ставит `break-before:page` — секция начнётся с
новой страницы; без него они льются вплотную и экономят бумагу.
⚠ CSS живёт в одном `<style>`: переопределение — своим `<style>` после,
а не правкой строки в шаблоне.
"""
import argparse
import json

_CSS = """
  @page { size:A4; margin:17mm 16mm 18mm 16mm;
    @bottom-left { content:"__TITLE__"; font-family:"DejaVu Sans", Arial, sans-serif;
      font-size:7.5pt; color:#8a92a3; }
    @bottom-right { content:counter(page) " / " counter(pages);
      font-family:"DejaVu Sans", Arial, sans-serif; font-size:7.5pt; color:#8a92a3; } }
  @page :first { @bottom-left { content:none; } @bottom-right { content:none; } }
  :root { --ink:#1d2330; --muted:#5b6475; --line:#d9dde5; --accent:__ACCENT__;
    --soft:#f3f5f9; --warn:#a4570a; --warnbg:#fff6ea; --ok:#1f7a45; --okbg:#eaf6ef;
    --bad:#b3261e; }
  * { box-sizing:border-box; }
  body { font-family:"DejaVu Sans", Arial, sans-serif; color:var(--ink); background:#fff;
    font-size:10.3pt; line-height:1.55; margin:0; }
  a { color:inherit; text-decoration:none; }
  p { margin:0 0 3mm; }
  ul, ol { margin:0 0 3mm; padding-left:6mm; }
  li { margin:0 0 1.2mm; }
  code { font-family:"DejaVu Sans Mono", monospace; font-size:8.6pt; background:var(--soft);
    padding:0 1.2mm; border-radius:2px; }
  table { width:100%; border-collapse:collapse; margin:2mm 0 5mm; font-size:9.2pt; }
  th, td { border:1px solid var(--line); padding:2mm 2.6mm; text-align:left; vertical-align:top; }
  th { background:var(--soft); font-weight:600; }
  tr { break-inside:avoid; }
  .y { color:var(--ok); font-weight:600; } .n { color:var(--bad); font-weight:600; }
  .p { color:var(--warn); font-weight:600; }
  .tag { display:inline-block; font-size:7.8pt; font-weight:600; padding:.3mm 1.8mm;
    border-radius:6px; margin-right:1mm; white-space:nowrap; }
  .stage { background:var(--warnbg); color:var(--warn); }
  .prod { background:var(--okbg); color:var(--ok); }
  .api { background:#e9eefc; color:var(--accent); } .ui { background:#f1e9fc; color:#6b2fd3; }

  /* части и секции */
  .part { break-before:page; margin:0 0 7mm; }
  .part .num { color:var(--accent); font-size:9pt; font-weight:700;
    letter-spacing:.8px; text-transform:uppercase; }
  .part h2 { font-size:17pt; margin:1mm 0 2mm; line-height:1.25; }
  .part p.sub { color:var(--muted); margin:0; font-size:10pt; }
  .part hr { border:none; border-top:2px solid var(--accent); margin:4mm 0 0; }
  h3 { font-size:12.5pt; margin:0 0 4mm; padding-bottom:1.6mm;
    border-bottom:1px solid var(--line); break-after:avoid; line-height:1.3; }
  h3 .no { color:var(--accent); margin-right:2mm; }
  .page { break-before:page; }
  section { break-inside:auto; margin:0 0 6mm; }
  .card { border:1px solid var(--line); border-radius:3px; padding:3mm 4mm;
    margin:0 0 4mm; break-inside:avoid; }
  .card h4 { margin:0 0 1.5mm; font-size:10pt; }

  /* обложка */
  h1 { font-size:22pt; margin:6mm 0 3mm; letter-spacing:-.3px; line-height:1.22; }
  .cover-meta { color:var(--muted); font-size:9.5pt; margin-bottom:7mm; }
  .lead { background:var(--soft); border-left:3px solid var(--accent);
    padding:4mm 5mm; margin:0 0 5mm; }
  .note { background:var(--warnbg); border-left:3px solid var(--warn);
    padding:3.5mm 5mm; margin:0 0 6mm; }
  .legend { border:1px solid var(--line); border-radius:3px; padding:3.5mm 4.5mm;
    font-size:9pt; }
  .legend h4 { margin:0 0 2mm; font-size:9.5pt; }
  .legend table { margin:0; font-size:8.8pt; }
  .legend td, .legend th { padding:1.4mm 2.2mm; }

  /* оглавление */
  .toc-title { font-size:14pt; font-weight:700; margin:2mm 0 4mm;
    padding-bottom:1.6mm; border-bottom:2px solid var(--accent); }
  .toc { list-style:none; padding:0; margin:0; }
  .toc li { display:flex; align-items:baseline; margin:0; padding:1.15mm 0; }
  .toc li.lvl1 { font-weight:700; margin-top:3mm; font-size:10.6pt; }
  .toc li.lvl2 { padding-left:6mm; font-size:9.6pt; }
  .toc .t { white-space:nowrap; }
  .toc .dots { flex:1; border-bottom:1px dotted #aab1bf; margin:0 2mm;
    transform:translateY(-1.2mm); }
  .toc .pg { font-variant-numeric:tabular-nums; min-width:7mm; text-align:right; }

  /* цифры */
  .stat { display:grid; grid-template-columns:repeat(4, 1fr); gap:4mm; margin:2mm 0 7mm; }
  .stat div { background:var(--soft); border-radius:3px; padding:3mm; text-align:center; }
  .stat b { display:block; font-size:16pt; color:var(--accent); }
  .stat span { font-size:8pt; color:var(--muted); }
"""


def html_report(spec: dict) -> str:
    """HTML-скелет печатного отчёта по `spec`; вернуть строку whole-document.

    Оглавление строится из частей и секций; у каждой строки пустой
    `<span class="pg" data-for="...">` — номер проставит `pdf_build_toc`.
    Пустые поля spec просто не рисуются (обложка без lead — только заголовок).

    Args:
        spec: {'title', 'meta', 'lead', 'legend', 'note', 'accent', 'parts'};
            строки — HTML-фрагменты автора, см. докстринг модуля.

    Returns:
        Полный HTML (doctype + style + обложка + оглавление + части).
    """
    parts = spec.get('parts', [])
    css = (_CSS.replace('__ACCENT__', spec.get('accent', '#2f5bd3'))
              .replace('__TITLE__', _footer_escape(spec.get('title', ''))))
    toc, body = [], []
    for part in parts:
        num = f"{part.get('num', '')} " if part.get('num') else ''
        toc.append(f'<li class="lvl1"><a href="#{part["id"]}">'
                   f'<span class="t">{num}{part["title"]}</span></a>'
                   f'<span class="dots"></span>'
                   f'<span class="pg" data-for="{part["id"]}"></span></li>')
        inner = []
        for sec in part.get('sections', []):
            sno = f'<span class="no">{sec["num"]}.</span>' if sec.get('num') else ''
            prefix = f'{sec["num"]}. ' if sec.get('num') else ''
            wrap = '<div class="page">' if sec.get('page') else ''
            inner.append(f'{wrap}<section id="{sec["id"]}">'
                         f'<h3>{sno}{sec["title"]}</h3>{sec.get("body", "")}</section>'
                         + ('</div>' if wrap else ''))
            toc.append(f'<li class="lvl2"><a href="#{sec["id"]}">'
                       f'<span class="t">{prefix}{sec["title"]}</span></a>'
                       f'<span class="dots"></span>'
                       f'<span class="pg" data-for="{sec["id"]}"></span></li>')
        sub = f'<p class="sub">{part["sub"]}</p>' if part.get('sub') else ''
        body.append(f'<div class="part" id="{part["id"]}">'
                    f'<div class="num">{"Часть " + str(part["num"]) if part.get("num") else ""}</div>'
                    f'<h2>{part["title"]}</h2>{sub}<hr></div>' + ''.join(inner))
    cover = [f'<h1>{spec.get("title", "")}</h1>']
    if spec.get('meta'):
        cover.append(f'<div class="cover-meta">{spec["meta"]}</div>')
    if spec.get('lead'):
        cover.append(f'<div class="lead">{spec["lead"]}</div>')
    if spec.get('note'):
        cover.append(f'<div class="note">{spec["note"]}</div>')
    if spec.get('legend'):
        cover.append(f'<div class="legend"><h4>Легенда</h4>{spec["legend"]}</div>')
    return ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            f'<title>{spec.get("title", "")}</title>\n<style>{css}</style>\n</head>\n'
            '<body>\n' + ''.join(cover) +
            '<div class="page"><div class="toc-title">Содержание</div><ul class="toc">'
            + ''.join(toc) + '</ul></div>\n' + ''.join(body) + '\n</body>\n</html>\n')


def _footer_escape(title: str) -> str:
    """Заголовок в CSS `content:"…"`: кавычки и бэкслэши экранировать."""
    return title.replace('\\', '\\\\').replace('"', '\\"')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='python -m html_.html_report',
                                 description='HTML-скелет печатного QA-отчёта')
    ap.add_argument('spec', help='JSON-файл со spec (см. докстринг модуля)')
    ap.add_argument('-o', '--out', required=True)
    ns = ap.parse_args()
    with open(ns.spec, encoding='utf-8') as fh:
        html = html_report(json.load(fh))
    with open(ns.out, 'w', encoding='utf-8') as fh:
        fh.write(html)
    print(ns.out)

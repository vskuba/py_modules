"""
Оглавление в PDF проставляется само: печать → поиск заголовков по тексту
страниц → правка номеров → перепечатка, пока числа не сойдутся.

QA-документы печатались вручную в два прохода (docs/pdf_tooling.md): скрипт
`build.py` на 25 строк с словарём «id → текст заголовка», который переписывали
каждый раз, когда правка сдвигала вёрстку, — а словарь после смены заголовка
протухал молча. Здесь вход только HTML: текст для поиска берётся из соседнего
`<span class="t">` той же строки оглавления, словарь не нужен вовсе.

Разметка-контракт (её же рисует `html_.html_report`):

    <li class="lvl2"><a href="#s-num"><span class="t">1. Покрытие в цифрах</span></a>
        <span class="dots"></span><span class="pg" data-for="s-num"></span></li>

Раунды: напечатать → по текстовому слою (pypdf, `pdf_text`) найти для каждой
строки первую страницу с её заголовком → вписать номера в `<span class="pg">`
→ перепечатать. Сходится за 2 раунда (первый печатает пустые номера, второй
подтверждает, что вписанные числа не сдвинули вёрстку); третий — страховка.

⚠ Страница оглавления содержит все заголовки разом — она выдаётся за каждую
строку. Страницы, где нашлась больше половины строк, объявляются оглавлением
и из поиска исключаются; если оглавление двухстраничное или дублируется в
приложениях, докиньте `skip_pages` (номера страниц с 1).
⚠ pypdf вынимает кириллический текст «по букве через пробел» («С К У Б А»,
грабля 7 docs/pdf_tooling.md), поэтому сравнение идёт по тексту без пробелов;
из-за того же поиск слеп к переносам: заголовок, разорванный на две строки,
встретится только если PDF отдаёт его одной.
⚠ Номер — первая страница после начала поиска: повтор заголовка в контенте
(ссылка в тексте, колонтитул) ниже первой не учитывается.
"""
import argparse
import json
import re

PDF_TOC_ROUNDS = 3  # раундов «печать → правка»; больший — просто дороже

_SPAN = re.compile(r'(<span class="pg" data-for="([^"]+)"[^>]*>)([^<]*)(</span>)')
_LI = re.compile(r'<li\b.*?</li>', re.S)
_T = re.compile(r'<span class="t"[^>]*>(.*?)</span>', re.S)
_FIND = re.compile(r'data-find="([^"]*)"')
_TAGS = re.compile(r'<[^>]+>')


def pdf_toc_entries(html: str) -> list:
    """Строки оглавления из HTML: [{'id', 'find'}] в порядке документа.

    Текст ищется в `<span class="t">` той же строки `<li>`; `data-find="..."`
    на span перебивает его — для заголовков, которые в контенте звучат иначе,
    чем в оглавлении.
    """
    out = []
    for li in _LI.finditer(html):
        span = _SPAN.search(li.group(0))
        if not span:
            continue
        own = _FIND.search(span.group(1))
        if own:
            find = own.group(1)
        else:
            t = _T.search(li.group(0))
            find = _TAGS.sub(' ', t.group(1)) if t else ''
        out.append({'id': span.group(2), 'find': _condense(find)})
    return out


def pdf_build_toc(html_path: str, out_pdf: str, rounds: int = PDF_TOC_ROUNDS,
                  print_fn=None, chrome: str | None = None,
                  skip_pages: tuple = ()) -> dict:
    """Довести HTML до PDF с верными номерами оглавления; вернуть {'pages','rounds','unresolved'}.

    Каждый раунд печатает документ, читает текстовый слой и сверяет номера.
    Совпали — PDF уже напечатан с финального HTML, готово. Нет — вписать,
    перепечатать. Раунды вышли — последний принт всё равно свежий, а
    расхождение видно в `unresolved` и по `converged`.

    Args:
        html_path: документ с пустыми (или устаревшими) `<span class="pg">`;
            правится на месте — это источник, его и отдавать в печать.
        out_pdf: куда печатать.
        rounds: раундов правки; 2 хватает заведомо сходящемуся документу.
        print_fn: `fn(html_path, out_pdf)`; по умолчанию `pdf_print_html`
            (с `chrome=`); своя функция позволяет подставить `pdf_print_seeded`.
        skip_pages: страницы, где искать нельзя (оглавление в двух местах),
            номера с 1.
        chrome: бинарь Chrome для печати по умолчанию.

    Returns:
        {'pages': {id: номер_страницы_или_None}, 'rounds': сколько раундов
        печаталось, 'converged': bool, 'unresolved': [id]}.

    Raises:
        RuntimeError: печать не удалась (из `print_fn`).
    """
    from pdf_.pdf_ import pdf_info, pdf_print_html, pdf_text

    do_print = print_fn or (lambda h, p: pdf_print_html(h, p, chrome=chrome))
    skip = set(skip_pages)
    pages_found: dict = {}
    converged = False
    r = 0  # раунды вышли нулём — не бывает, но r в отчёте обязан быть числом
    for r in range(1, rounds + 1):
        do_print(html_path, out_pdf)
        n = pdf_info(out_pdf)['pages']
        texts = [_condense(pdf_text(out_pdf, i)) for i in range(n)]
        with open(html_path, encoding='utf-8') as fh:
            html = fh.read()
        entries = pdf_toc_entries(html)
        if not entries:
            raise RuntimeError(f'в {html_path} нет строк оглавления '
                               f'(<span class="pg" data-for=…>)')
        # страница, где собралась больше половины строк, — само оглавление
        hits = [sum(1 for e in entries if e['find'] and e['find'] in txt)
                for txt in texts]
        toc = {i for i, h in enumerate(hits) if h > len(entries) / 2 and h >= 2}
        current = {m.group(2): m.group(3) for m in _SPAN.finditer(html)}
        pages_found = {}
        for e in entries:
            hit = next((i + 1 for i, txt in enumerate(texts)
                        if i + 1 not in skip and i not in toc
                        and e['find'] and e['find'] in txt), None)
            pages_found[e['id']] = hit
        written = {k: (int(current[k]) if current.get(k, '').isdigit() else None)
                   for k in pages_found}
        if written == pages_found:  # PDF напечатан с HTML, где эти номера уже стоят
            converged = True
            break
        html = _SPAN.sub(lambda m: f'{m.group(1)}'
                         f'{pages_found.get(m.group(2)) or "?"}'
                         f'{m.group(4)}', html)
        with open(html_path, 'w', encoding='utf-8') as fh:
            fh.write(html)
    if not converged and r >= rounds:
        do_print(html_path, out_pdf)  # финальный PDF — с финального HTML
    return {'pages': pages_found, 'rounds': r, 'converged': converged,
            'unresolved': [k for k, v in pages_found.items() if v is None]}


def _condense(text: str) -> str:
    """Текст без пробельных символов: «С К У Б А» от pypdf снова «СКУБА»."""
    return re.sub(r'\s+', '', text)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='python -m pdf_.pdf_toc',
                                 description='PDF с проставленными номерами оглавления')
    ap.add_argument('html', help='документ со <span class="pg" data-for=…>; правится на месте')
    ap.add_argument('out_pdf')
    ap.add_argument('--rounds', type=int, default=PDF_TOC_ROUNDS)
    ap.add_argument('--skip', default='', metavar='1,2',
                    help='страницы, где искать нельзя (дубликаты оглавления), с 1')
    ap.add_argument('--chrome', default=None)
    ns = ap.parse_args()
    skip = tuple(int(v) for v in ns.skip.split(',')) if ns.skip else ()
    print(json.dumps(pdf_build_toc(ns.html, ns.out_pdf, rounds=ns.rounds,
                                   chrome=ns.chrome, skip_pages=skip),
                     ensure_ascii=False, indent=1))

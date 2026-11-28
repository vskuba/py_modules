"""
Перевод HTML-отчёта между языками локальной моделью (GX10), секция за секцией.

Отчёт, собранный `html_report`/`pdf_report`, переводят машинным переводом
нельзя: в теле живут `<code>`, JSON-фрагменты, URL, uuid, имена полей и
продуктов — переводчик, добравшись до них, ломает и текст, и разметку. Здесь
переводит локальная модель (`~/.claude/bin/gx10`: материал через stdin,
инструкция аргументом), но по частям и под охраной: `<head>` со стилями не
трогается вовсе, `<body>` режется перед каждым `<h2>` (секция = кусок = запрос),
а после каждого куска мультимножество тегов и число `<img>` сверяются с
оригиналом — разъехалось, один повторный запрос, не сошлось снова, падение со
списанием различий. Молча перевранный отчёт хуже упавшего.

Термины, которым переводить нельзя или надо переводить фиксированно, задаются
извне: `keep` — список продуктовых имён, `glossary` — пару «термин = перевод»
файлом (`--glossary terms.txt`: JSON-объект или построчно `термин = перевод`).

CLI:

    python -m html_.html_translate report.html --lang ru
    python -m html_.html_translate report.html --out report.ru.html --lang ru \\
        --glossary terms.txt --keep HeroPayments --keep "Brand Settings"
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter

HTML_TRANSLATE_BIN = os.environ.get('GX10_BIN', os.path.expanduser('~/.claude/bin/gx10'))
HTML_TRANSLATE_LANGS = {'ru': 'Russian', 'en': 'English', 'uk': 'Ukrainian',
                        'de': 'German', 'fr': 'French', 'es': 'Spanish', 'pl': 'Polish'}
HTML_TRANSLATE_TOKENS = 6000   # секция отчёта с таблицей не влезает в дефолтные 4000
HTML_TRANSLATE_TIMEOUT = 600   # секунд на кусок: локальная модель медленная, но верная
# Место реза: перед каждым заголовком секции; `[\s>]` — чтобы `<h2x>` не резалось.
HTML_TRANSLATE_SPLIT = re.compile(r'(?=<h2[\s>])', flags=re.I)
# Мультимножество тегов: `div` и `/div` считаются раздельно — закрытый тег не
# подставит отсутствующий открывающий.
HTML_TRANSLATE_TAG = re.compile(r'</?[a-zA-Z][a-zA-Z0-9]*')

HTML_TRANSLATE_SYSTEM = """You translate HTML fragments of a {src} test report into {dst}. \
Translate ONLY human-readable text. Keep EVERY HTML tag, attribute, class, src, href exactly as is. \
Do NOT translate or change anything inside <code>…</code>, JSON, URLs, file names, uuids, ids, \
numbers, currency codes, field names, commit hashes, branch names, and the words PASS / FAIL. \
{keep}{glossary}\
Use natural technical {dst}. Output ONLY the translated HTML fragment, no explanations, no markdown fences."""


def html_translate_html(html_text: str, lang: str = 'ru', call=None, glossary=(),
                        keep=(), src: str = 'en') -> dict:
    """Перевести HTML-отчёт по кускам; вернуть {'html', 'chunks', 'retried'}.

    Кусок — часть `<body>` от одного `<h2>` до следующего (голова со стилями и
    хвост за `</body>` не участвуют вовсе). `call(text, system)` — переводчик:
    по умолчанию `html_translate_gx10`; подменяется в тестах. После каждого
    куска — сверка мультимножества тегов и числа `<img>`; не сходится — кусок
    запрашивается ещё раз, не сходится и тогда — RuntimeError со списком
    различий (отчёт с молча потерянной разметкой не отдаётся).

    Args:
        html_text: исходный HTML целиком.
        lang: целевой язык (`HTML_TRANSLATE_LANGS`, либо код пойдёт как есть).
        call: `fn(text, system) -> str`; перевод одного куска.
        glossary: {'термин': 'перевод'} или ['термин = перевод'] — фиксированные соответствия.
        keep: слова, которые переводить нельзя (продуктовые имена).
        src: исходный язык подсказки модели.
    """
    call = call or html_translate_gx10
    system = _system(lang, glossary, keep, src)
    head, _, rest = html_text.partition('<body')
    if not rest:
        raise ValueError('в исходнике нет <body> — резать по секциям нечего')
    open_attr, gt, body_tail = rest.partition('>')
    body, closed, tail = body_tail.rpartition('</body>')
    if not closed:  # закрытия нет — телом считается всё до конца файла
        body, tail = body_tail, ''
    chunks = HTML_TRANSLATE_SPLIT.split(body)
    done, retried = [], []
    for i, chunk in enumerate(chunks, 1):
        if not chunk.strip():
            done.append(chunk)
            continue
        out = call(chunk, system)
        for attempt in (1, 2):
            diffs = _diff(chunk, out)
            if not diffs:
                break
            if attempt == 1:
                retried.append(i)
                out = call(chunk, system)
        if diffs:
            raise RuntimeError(f'кусок {i}/{len(chunks)} переведён с потерей разметки: '
                               + '; '.join(diffs))
        done.append(out)
    return {'html': (_set_lang(head, lang) + '<body' + open_attr + gt + ''.join(done)
                     + ('</body>' if closed else '') + tail),
            'chunks': len(chunks), 'retried': retried}


def html_translate(src_file: str, out_file: str = '', lang: str = 'ru', glossary_file: str = '',
                   keep=(), src: str = 'en', call=None) -> dict:
    """Файл HTML-отчёта → тот же файл переведённым (`--out`, по умолчанию `<имя>.<lang>.html`)."""
    out_file = out_file or (os.path.splitext(src_file)[0] + f'.{lang}.html')
    glossary = _read_glossary(glossary_file) if glossary_file else ()
    with open(src_file, encoding='utf-8') as fh:
        html_text = fh.read()
    got = html_translate_html(html_text, lang=lang, call=call, glossary=glossary, keep=keep, src=src)
    with open(out_file, 'w', encoding='utf-8') as fh:
        fh.write(got['html'])
    return {**got, 'out': out_file}


def html_translate_gx10(text: str, system: str, bin_path: str = HTML_TRANSLATE_BIN,
                        timeout: float = HTML_TRANSLATE_TIMEOUT) -> str:
    """Один кусок через gx10: материал в stdin, инструкция — аргументом; ответ без ограждения кода."""
    instruction = 'Translate this HTML fragment following the rules. Keep every tag and attribute.'
    try:
        proc = subprocess.run([bin_path, '-s', system, '-t', str(HTML_TRANSLATE_TOKENS),
                               '--timeout', str(int(timeout)), instruction],
                              input=text, capture_output=True, text=True, timeout=timeout + 60)
    except FileNotFoundError:
        raise RuntimeError(f'нет бинаря модели: {bin_path} (GX10_BIN?)') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'моделка молчит дольше {timeout} s — правь timeout') from None
    answer = re.sub(r'^```(?:html)?\s*|\s*```\s*$', '', proc.stdout.strip())
    if not answer:
        raise RuntimeError(f'пустой ответ модели: {proc.stderr[-300:]}')
    return answer


# ── приватное ──


def _system(lang: str, glossary, keep, src: str) -> str:
    dst = HTML_TRANSLATE_LANGS.get(lang, lang)
    src_name = HTML_TRANSLATE_LANGS.get(src, src)
    keep_part = (f'Product names must stay exactly as listed: {", ".join(keep)}. ' if keep else '')
    if glossary:
        pairs = glossary if isinstance(glossary, str) else _glossary_lines(glossary)
        glossary_part = ('Fixed terms — render exactly as given: ' + '; '.join(pairs) + '. ')
    else:
        glossary_part = ''
    return (HTML_TRANSLATE_SYSTEM.format(dst=dst, src=src_name, keep=keep_part,
                                         glossary=glossary_part))


def _glossary_lines(glossary) -> list:
    if isinstance(glossary, dict):
        return [f'{k} = {v}' for k, v in glossary.items()]
    return [str(one) for one in glossary]


def _read_glossary(path: str) -> dict:
    """Словарь фиксированных соответствий: JSON-объект или построчно `термин = перевод`."""
    text = open(path, encoding='utf-8').read()
    try:
        got = json.loads(text)
        if isinstance(got, dict):
            return {str(k): str(v) for k, v in got.items()}
        return {str(one): '' for one in got}  # массив — просто список «не трогать»
    except ValueError:
        pass
    pairs = {}
    for line in text.splitlines():
        term, eq, value = line.partition('=')
        if eq and term.strip():
            pairs[term.strip()] = value.strip()
    return pairs


def _tag_multiset(text: str) -> Counter:
    return Counter(HTML_TRANSLATE_TAG.findall(text))


def _diff(src: str, out: str) -> list:
    """Чем переведённый кусок отличается по разметке от оригинала: пусто — цел."""
    diffs = []
    src_tags, out_tags = _tag_multiset(src), _tag_multiset(out)
    for tag in sorted(set(src_tags) | set(out_tags)):
        if src_tags[tag] != out_tags[tag]:
            diffs.append(f'<{tag}>: было {src_tags[tag]}, стало {out_tags[tag]}')
    if src.lower().count('<img') != out.lower().count('<img'):
        diffs.append(f'<img>: было {src.lower().count("<img")}, стало {out.lower().count("<img")}')
    return diffs


def _set_lang(head: str, lang: str) -> str:
    """атрибут lang у <html> — целевому языку; тега нет — не выдумываем."""
    def fix(match):
        attrs = re.sub(r'\slang\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', match.group(1))
        return f'<html{attrs} lang="{lang}">'
    return re.sub(r'<html\b([^>]*)>', fix, head, count=1, flags=re.I)


def _cli(argv=None):
    ap = argparse.ArgumentParser(prog='python -m html_.html_translate',
                                 description='перевод HTML-отчёта локальной моделью, секция за секцией')
    ap.add_argument('src', help='файл HTML-отчёта')
    ap.add_argument('--out', default='', help='куда лечь переводу; по умолчанию <имя>.<lang>.html')
    ap.add_argument('--lang', default='ru', help='целевой язык (ru, en, uk, …)')
    ap.add_argument('--src-lang', default='en', help='исходный язык подсказки модели')
    ap.add_argument('--glossary', default='', metavar='FILE',
                    help='фиксированные соответствия: JSON-объект или построчно «термин = перевод»')
    ap.add_argument('--keep', action='append', default=[],
                    help='слово, которое переводить нельзя (продуктовое имя); можно несколько')
    ns = ap.parse_args(argv)
    try:
        got = html_translate(ns.src, ns.out, lang=ns.lang, glossary_file=ns.glossary,
                             keep=ns.keep, src=ns.src_lang)
    except (RuntimeError, ValueError, OSError) as exc:  # отказ — строка, не трейсбек
        print(f'отказ: {exc}', file=sys.stderr)
        return 1
    print(f"{got['out']}: кусков {got['chunks']}, повторов {len(got['retried'])}")
    return 0


if __name__ == '__main__':
    sys.exit(_cli())

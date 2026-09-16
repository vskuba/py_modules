"""
Классы, которые JS лепит, против CSS, который о них знает: дыра видна числом.

Страницы панелей живут разметкой из JS (template literal'ы, `classList.toggle`)
и стилями шаблона; расходятся они молча — «js-класс без css» не роняет ничего,
просто выглядит не так и ловится глазами позже. Функция раскладывает пару
шаблон ↔ скрипты и отвечает числом с каждой стороны: что лепит JS и не описано
в CSS; что CSS знает и никому не нужно; какие id из JS не встали в разметку.

Грабли сверки: разметка в template literal'ах пестрит `${...}` — выражения
вырезаются целиком и условные классы из них не извлекаются (лучше недобор,
чем `${x ? 'done' : ''}` считалось классом); пары подаются как поданы — класс
из соседнего общего css-файла, не переданного в аргументы, честно встанет в
`js_unstyled`.
"""
import argparse
import re
from pathlib import Path

_STYLE = re.compile(r'<style[^>]*>(.*?)</style>', re.S | re.I)
_SCRIPT = re.compile(r'<script[^>]*>(.*?)</script>', re.S | re.I)
_CLASS_SEL = re.compile(r'\.([A-Za-z][\w-]*)')
_ATTR_CLASS = re.compile(r'class="([^"]*)"')
_ATTR_ID = re.compile(r'id="([\w-]+)"')
_JS_LIST = re.compile(r"classList\.\w+\(\s*(\[[^\]]*\]|['\"][^'\"]+['\"])")
_JS_ON = re.compile(r"className\s*=\s*['\"]([^'\"]+)['\"]")
_JS_ID = re.compile(r"getElementById\(['\"]([\w-]+)|querySelector(?:All)?\("
                    r"['\"]#([\w-]+)")


def _tokens(blob: str) -> set:
    """Имена классов из куска разметки: `${...}`-выражения вырезаются целиком
    (условные классы из них не извлекаем — это честная потеря, не мусор);
    годятся только осмысленные имена, не обрывки кода."""
    clean = re.sub(r'\$\{[^{}]*\}', ' ', blob)
    return {t for t in re.split(r"[\s'\"]+", clean)
            if re.fullmatch(r'[A-Za-z][\w-]*', t)}


def html_scan_pairs(html_paths, js_paths=()) -> dict:
    """Пара шаблон ↔ скрипты: чем разъехались классы и id.

    Args:
        html_paths: шаблоны (с инлайн-`<style>` и разметкой).
        js_paths: скрипты; пусто — только инлайн-скрипты шаблонов.

    Returns:
        {'js_unstyled': классы из JS без CSS, 'css_unused': селекторы CSS,
         о которых не знает ни JS, ни разметка, 'ids_missing': id из JS,
         которых нет в разметке}.
    """
    html_text = '\n'.join(Path(p).read_text(encoding='utf-8')
                          for p in map(str, html_paths))
    js_text = '\n'.join(Path(p).read_text(encoding='utf-8')
                       for p in map(str, js_paths)) + '\n' + \
        '\n'.join(_SCRIPT.findall(html_text))

    css = set()
    for block in _STYLE.findall(html_text):
        for sel in (chunk.rsplit('}', 1)[-1] for chunk in block.split('{')):
            css.update(_CLASS_SEL.findall(sel))
    markup_cls = set()
    for val in _ATTR_CLASS.findall(html_text):
        markup_cls |= _tokens(val)
    markup_id = set(_ATTR_ID.findall(html_text))

    js_cls = set()
    for blob in (_JS_LIST.findall(js_text) + _JS_ON.findall(js_text)
                 + _ATTR_CLASS.findall(js_text)):
        js_cls |= _tokens(blob)
    js_id = {g for tup in _JS_ID.findall(js_text) for g in tup if g}

    return {'js_unstyled': sorted(js_cls - css - markup_cls),
            'css_unused': sorted(css - js_cls - markup_cls),
            'ids_missing': sorted(js_id - markup_id)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Классы/ид из JS против шаблона: чем разъехались.')
    ap.add_argument('html', nargs='+', help='шаблон(ы)')
    ap.add_argument('--js', default='', help='скрипты через запятую')
    ns = ap.parse_args()
    try:
        res = html_scan_pairs(ns.html, ns.js.split(',') if ns.js else ())
        for key in ('js_unstyled', 'css_unused', 'ids_missing'):
            print(f'{key} ({len(res[key])}): {", ".join(res[key]) or "—"}')
    except (RuntimeError, ValueError, OSError) as err:
        raise SystemExit(f'ошибка: {err}')

"""Ложные друзья и кальки перевода ru→uk: поиск по словарю — то, чего не видит LanguageTool.

LanguageTool (`text_/text_grammar.py`) ловит слова, которых нет в словаре, и
согласование. Но перевод с русского ломается иначе: слово есть в украинском,
только значит другое. «Лист» — письмо, а не вкладка таблицы; «маєток» —
поместье, а не property аналитики; «сварка» — ссора, а не зварювання;
«контактне обличчя» — лицо как часть тела. Словарь проверку проходит, смысл
нет. Здесь такие места ищутся правилами — регулярками со справкой.

```bash
python -m translate_.translate_falsefriend scan site/index.html site/warning.html
python -m translate_.translate_falsefriend scan site/index.html --fixes auto.json   # однозначные правки
python -m translate_.translate_falsefriend scan --text "Правило листа"
```

## Правила — данные, а не код

`translate_falsefriend_ru_uk.json` рядом: `[{re, why, to, hint}]`. `to` задан —
правка однозначна, `--fixes` её выпишет готовой для
`text_grammar apply`; `to: null` — место только показывается с подсказкой
`hint`, решение за читающим (у «ні одного» десяток форм, у «по + давальний»
половина случаев законна). Новый ложный друг — строка в JSON, без правки кода;
`--rules` подставляет свой словарь (другая пара языков).

Текст HTML берётся тот же, что у `text_grammar`: узлы, атрибуты `title`/`alt`,
литералы JS с кириллицей — разметку и код правило не увидит.

## ⚠ Чего модуль НЕ делает

**Не отличает цитату от авторского текста.** «Звоните» в цитате объявления —
верно, в тексте автора — нет. Правило выдаёт оба; цитаты в «…» обходит тот,
кто читает отчёт.

**Не ищет новое.** Ловит только то, что уже в словаре. Неизвестные ошибки
смысла ищет `translate_review` (модель), найденные там типовые — переносите
сюда правилом.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from text_.text_grammar import text_grammar_blocks

TRANSLATE_FALSEFRIEND_RULES = os.path.join(os.path.dirname(__file__), 'translate_falsefriend_ru_uk.json')


def translate_falsefriend_rules(path: str = TRANSLATE_FALSEFRIEND_RULES) -> list[dict]:
    """Прочитать словарь правил и скомпилировать регулярки; битое правило — отказ с его номером."""
    rules = json.load(open(path, encoding='utf-8'))
    for i, r in enumerate(rules):
        try:
            r['_re'] = re.compile(r['re'])
        except re.error as e:
            raise ValueError(f"правило #{i} ({r['re']!r}): {e}") from e
    return rules


def translate_falsefriend_scan_text(text: str, rules: list[dict], line: int = 1,
                                    file: str = '<text>') -> list[dict]:
    """Найти места по правилам в одном тексте: [{file, line, word, why, to, hint, context}]."""
    out = []
    for r in rules:
        for m in r['_re'].finditer(text):
            s, e = m.span()
            out.append({'file': file, 'line': line, 'word': m.group(0), 'why': r['why'],
                        'to': r.get('to'), 'hint': r.get('hint', ''),
                        'context': text[max(0, s - 50):s] + '⟦' + m.group(0) + '⟧' + text[e:e + 50]})
    return out


def translate_falsefriend_scan(path: str, rules: list[dict] | None = None) -> list[dict]:
    """Пройти видимый текст файла (.html — узлы, атрибуты, литералы JS) по словарю правил."""
    rules = rules or translate_falsefriend_rules()
    raw = open(path, encoding='utf-8').read()
    blocks = text_grammar_blocks(raw) if path.endswith(('.html', '.htm')) else \
        [{'text': raw, 'line': 1}]
    found = []
    for b in blocks:
        found += translate_falsefriend_scan_text(b['text'], rules, b['line'], path)
    return found


def translate_falsefriend_fixes(found: list[dict]) -> list[dict]:
    """Однозначные правки (у правила задан `to`) — в формате `text_grammar apply`."""
    seen, out = set(), []
    for f in found:
        if f['to'] and f['word'] not in seen:
            seen.add(f['word'])
            to = f['to']
            if f['word'][:1].isupper() and to[:1].islower():
                to = to[:1].upper() + to[1:]
            out.append({'from': f['word'], 'to': to, 'why': f['why']})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='python -m translate_.translate_falsefriend',
                                 description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sc = sub.add_parser('scan', help='найти ложных друзей и кальки')
    sc.add_argument('files', nargs='*')
    sc.add_argument('--text')
    sc.add_argument('--rules', default=TRANSLATE_FALSEFRIEND_RULES, help='свой словарь JSON')
    sc.add_argument('--json', help='все находки — в файл')
    sc.add_argument('--fixes', help='однозначные правки — в файл для text_grammar apply')
    a = ap.parse_args(argv)

    rules = translate_falsefriend_rules(a.rules)
    found = []
    if a.text:
        found += translate_falsefriend_scan_text(a.text, rules)
    for p in a.files:
        found += translate_falsefriend_scan(p, rules)
    for f in found:
        tail = f"→ {f['to']}" if f['to'] else f"(подсказка: {f['hint']})"
        print(f"{f['file']}:{f['line']}: {f['context']}\n    {f['why']} {tail}")
    auto = translate_falsefriend_fixes(found)
    print(f"\nнаходок {len(found)}, из них однозначных правок {len(auto)}")
    if a.json:
        json.dump(found, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if a.fixes:
        json.dump(auto, open(a.fixes, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())

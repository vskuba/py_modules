"""Свести списки правок перевода в один — без конфликтов, промахов и порчи цифр и латиницы.

Правки приходят из нескольких рук: `text_grammar` (LanguageTool),
`translate_falsefriend` (словарь), `translate_review` (модель), субагенты по
кускам страницы. Прежде чем `text_grammar apply`, их надо свести, и каждый
раз всплывают одни и те же грабли — здесь они закрыты:

- **порядок**: короткая правка («Кіев→Київ»), применённая раньше длинной
  («Кіев, тел.→Київ, тел.»), делает длинную промахом. Сортировка — от
  длинного `from` к короткому;
- **конфликты**: одинаковый `from` с разными `to` — в отчёт, побеждает первый;
- **апостроф**: прямой `'` между буквами — в типографский `’` (и так
  выглядит украинский текст, и `'` в JS-литерале не ломает скрипт);
- **охрана**: правка, меняющая цифры, латиницу, IP или домен, — отброшена:
  перевод не трогает факты, а модель иной раз «чинит» и их;
- **попадания**: с `--file` каждая правка пробуется `apply --dry-run`; ноль
  попаданий — отбрасывается с пометкой.

```bash
python -m translate_.translate_fixes merge a.json b.json --file site/index.html --out merged.json
python -m text_.text_grammar apply site/index.html --fixes merged.json
```

Вход — JSON-списки `[{from, to, ...}]`; лишние поля (`why`, `line`) едут дальше.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile

from text_.text_grammar import text_grammar_apply

_APOS = re.compile(r"(?<=[Ѐ-ӿ])'(?=[Ѐ-ӿ])")
_FACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*")


def translate_fixes_guard(fix: dict) -> str:
    """Причина отказа правке ('' — годна): пустая, без изменений, или трогает цифры/латиницу."""
    a, b = fix.get('from', ''), fix.get('to', '')
    if not a.strip() or not isinstance(b, str):
        return 'пустая правка'
    if a == b:
        return 'from == to'
    if sorted(_FACT.findall(a)) != sorted(_FACT.findall(b)):
        return 'меняет цифры/латиницу'
    if len(b) > 3 * len(a) + 20:
        return 'замена несоразмерно длиннее'
    if len(a) > 160:
        # Модель иной раз возвращает весь абзац «исправленным»: такую правку не проверить
        # глазами и не применить точечно — пусть назовёт само место.
        return 'from длиннее 160 знаков'
    return ''


def translate_fixes_merge(lists: list[list[dict]], file: str | None = None) -> tuple[list[dict], list[dict]]:
    """Свести списки правок: (годные — от длинных к коротким, отброшенные — с причиной `reason`)."""
    kept: dict[str, dict] = {}
    dropped: list[dict] = []
    for fixes in lists:
        for f in fixes:
            f = dict(f)
            f['to'] = _APOS.sub('’', f.get('to') or '')
            reason = translate_fixes_guard(f)
            if reason:
                dropped.append(dict(f, reason=reason))
            elif f['from'] in kept:
                if kept[f['from']]['to'] != f['to']:
                    dropped.append(dict(f, reason=f"конфликт: уже → {kept[f['from']]['to']}"))
            else:
                kept[f['from']] = f
    out = sorted(kept.values(), key=lambda x: -len(x['from']))
    if file:
        with tempfile.NamedTemporaryFile(suffix='.' + file.rsplit('.', 1)[-1], delete=False) as tmp:
            pass
        shutil.copy(file, tmp.name)
        hits = text_grammar_apply(tmp.name, out, dry_run=True)
        good = []
        for f, h in zip(out, hits):
            if h['hits']:
                good.append(dict(f, hits=h['hits']))
            else:
                dropped.append(dict(f, reason='нет в тексте'))
        out = good
    return out, dropped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='python -m translate_.translate_fixes', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    mg = sub.add_parser('merge', help='свести списки правок')
    mg.add_argument('inputs', nargs='+')
    mg.add_argument('--file', help='проверить попадания на этом файле (dry-run)')
    mg.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    lists = [json.load(open(p, encoding='utf-8')) for p in a.inputs]
    good, dropped = translate_fixes_merge(lists, a.file)
    json.dump(good, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    for d in dropped:
        print(f"  ✗ {d['reason']}: {d.get('from')} → {d.get('to')}")
    print(f"правок {sum(map(len, lists))} → годных {len(good)}, отброшено {len(dropped)} → {a.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

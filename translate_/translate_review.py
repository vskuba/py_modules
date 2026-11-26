"""Смысловая вычитка перевода моделью: кандидаты правок {from, to, why}, а не переписанный текст.

Словарь (`translate_falsefriend`) ловит известные ошибки, LanguageTool —
орфографию. Остаётся то, что видно только по смыслу: «студія названа ім’ям
господині» про мужчину, «зібрані одним канатом тим», «Кожна працює без
будь-якої теми» вместо «незалежно від». Это дело редактора, и его здесь делает
модель — но под охраной: она не переписывает текст, а предлагает точечные
замены, и каждая проходит проверку, прежде чем попасть в список.

```bash
set -a; . ~/.claude/gx10.env; set +a          # адрес, ключ и GX10_MODEL локальной модели
.venv/bin/python -m translate_.translate_review site/index.html --out review.json
python -m translate_.translate_review site/index.html --lines 600-900 --model gx10/qwen-large
python -m translate_.translate_fixes merge review.json --file site/index.html --out ok.json
```

## Окружение

Вызов модели идёт через реестр провайдеров (`ai/provider`), а он тянет
`pydantic_ai` — в системном Python его нет. Запускать из `py_modules/.venv`.

## Охрана

- `from` обязан **дословно** стоять в куске, который видела модель, — иначе
  выдумка, отброшена;
- цифры, латиница, домены, IP в `from` и `to` совпадают (`translate_fixes_guard`);
- дословные цитаты («…») модель просят не трогать, но гарантии нет —
  выход смотрит человек или второй агент.

Текст режется на куски по ~2500 знаков по границам блоков HTML (те же места,
что у `text_grammar`: узлы, атрибуты, литералы JS), каждый кусок — отдельный
запрос, `--workers` разом.

## ⚠ Чего модуль НЕ делает

**Не применяет.** Итог — JSON для `translate_fixes merge` → `text_grammar
apply`. Модель ошибается (локальная Qwen в Q4 — заметно), поэтому список
читает человек или агент; модуль лишь сокращает чтение с 170 тысяч знаков до
сотни кандидатов.

**Не знает пару языков по умолчанию шире ru→uk.** `--src`/`--dst` меняют
подсказку модели, словарь ложных друзей — в `translate_falsefriend`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx

from text_.text_grammar import text_grammar_blocks
from translate_.translate_fixes import translate_fixes_guard

TRANSLATE_REVIEW_MODEL = 'gx10/qwen-large'
TRANSLATE_REVIEW_CHUNK = 2500

TRANSLATE_REVIEW_PROMPT = """You are a native {dst} copy editor. The text below was translated from {src} to {dst} and still contains translation errors.
Find ONLY real errors: {src} words or forms, false friends (word exists in {dst} but means something else here), calques, wrong case/gender agreement, garbled phrases that make no sense.
Do NOT touch: names of people, brands, domains, IPs, numbers, Latin words, deliberate IT jargon, and text inside «…» quotes that quote a source verbatim.
Do NOT restyle correct text. Minimal edits.
Return ONLY a JSON array, no prose: [{{"from": "exact substring copied from the text (3-8 words of context)", "to": "corrected substring", "why": "short reason in English"}}]
If nothing is wrong, return [].

TEXT (each block starts with its line number):
{text}"""


def translate_review_chunk(chunk: dict, model: str = TRANSLATE_REVIEW_MODEL, src: str = 'Russian',
                           dst: str = 'Ukrainian', timeout: float = 600) -> tuple[list[dict], list[dict]]:
    """Один кусок — один запрос: (принятые кандидаты, отброшенные с причиной `reason`)."""
    prompt = TRANSLATE_REVIEW_PROMPT.format(src=src, dst=dst, text=chunk['text'])
    ok, bad = [], []
    for c in _parse(_ask(model, prompt, timeout)):
        c = {'from': str(c['from']), 'to': str(c['to']), 'why': str(c.get('why', ''))}
        if c['from'] not in chunk['text']:
            bad.append(dict(c, reason='нет в тексте (выдумка модели)'))
            continue
        reason = translate_fixes_guard(c)
        if reason:
            bad.append(dict(c, reason=reason))
            continue
        pos = chunk['text'].find(c['from'])
        line = re.findall(r'^\[(\d+)\]', chunk['text'][:pos], re.M)
        ok.append(dict(c, line=int(line[-1]) if line else chunk['line']))
    return ok, bad


def translate_review_file(path: str, model: str = TRANSLATE_REVIEW_MODEL, lines: tuple[int, int] | None = None,
                          workers: int = 2, src: str = 'Russian', dst: str = 'Ukrainian',
                          size: int = TRANSLATE_REVIEW_CHUNK, progress=None) -> tuple[list[dict], list[dict]]:
    """Вычитать видимый текст файла моделью: (кандидаты правок, отброшенные охраной)."""
    raw = open(path, encoding='utf-8').read()
    blocks = text_grammar_blocks(raw) if path.endswith(('.html', '.htm')) else [{'text': p, 'line': i + 1}
                                                                   for i, p in enumerate(raw.split('\n\n'))]
    if lines:
        blocks = [b for b in blocks if lines[0] <= b['line'] <= lines[1]]
    blocks = [b for b in blocks if len(b['text'].strip()) > 2]
    chunks = _chunks(blocks, size)
    ok, bad = [], []

    def run(ch):
        try:
            return translate_review_chunk(ch, model, src, dst)
        except Exception as e:  # кусок не прошёл — остальные не теряем
            return [], [{'from': '', 'to': '', 'reason': f"кусок со строки {ch['line']}: {e}"}]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (o, b) in enumerate(pool.map(run, chunks), 1):
            ok += o
            bad += b
            if progress:
                progress(i, len(chunks), len(ok))
    return ok, bad


def _chunks(blocks: list[dict], size: int) -> list[dict]:
    out, cur, first = [], [], None
    for b in blocks:
        t = f"[{b['line']}] {b['text'].strip()}"
        if cur and sum(len(x) for x in cur) + len(t) > size:
            out.append({'line': first, 'text': '\n'.join(cur)})
            cur, first = [], None
        cur.append(t)
        first = first or b['line']
    if cur:
        out.append({'line': first, 'text': '\n'.join(cur)})
    return out


def _ask(model: str, prompt: str, timeout: float) -> str:
    from ai.provider.ai_provider_registry import ai_provider_registry_get
    provider = ai_provider_registry_get(model)
    if not provider:
        raise ValueError(f"модель '{model}' не найдена в реестре провайдеров")
    clean = model.partition('/')[2]
    base, key = provider.raw_endpoint(clean)
    if not base:
        raise ValueError(f"для '{model}' нет адреса — для gx10: set -a; . ~/.claude/gx10.env")
    # Имя в тело кладёт стратегия: vLLM знает только то, под которым поднял модель
    # (`GX10_MODEL` в ~/.claude/gx10.env), и на реестровый хвост отвечает 404.
    body = {'model': provider.raw_model(clean), 'temperature': 0.1, 'max_tokens': 4000,
            'messages': [{'role': 'user', 'content': prompt}]}
    body.update(provider.raw_body_no_thinking())
    r = httpx.post(f"{base.rstrip('/')}/chat/completions", json=body,
                   headers={'Authorization': f'Bearer {key}'}, timeout=timeout)
    r.raise_for_status()
    return ((r.json().get('choices') or [{}])[0].get('message') or {}).get('content') or ''


def _parse(answer: str) -> list[dict]:
    m = re.search(r'\[.*\]', answer, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict) and 'from' in d and 'to' in d]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='python -m translate_.translate_review', description=__doc__.split('\n')[0])
    ap.add_argument('file')
    ap.add_argument('--out', required=True, help='кандидаты правок JSON')
    ap.add_argument('--model', default=TRANSLATE_REVIEW_MODEL)
    ap.add_argument('--lines', help='диапазон строк файла: 600-900')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--src', default='Russian')
    ap.add_argument('--dst', default='Ukrainian')
    a = ap.parse_args(argv)
    rng = tuple(map(int, a.lines.split('-'))) if a.lines else None
    ok, bad = translate_review_file(a.file, a.model, rng, a.workers, a.src, a.dst,
                                    progress=lambda i, n, k: print(f"  кусок {i}/{n}, кандидатов {k}",
                                                                   file=sys.stderr, flush=True))
    json.dump(ok, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    for c in ok:
        print(f"{a.file}:{c['line']}: {c['from']} → {c['to']}   ({c['why']})")
    print(f"\nкандидатов {len(ok)}, отброшено охраной {len(bad)} → {a.out}")
    for b in bad[:20]:
        print(f"  ✗ {b['reason']}: {b.get('from')} → {b.get('to')}")
    failed = sum(1 for b in bad if b['reason'].startswith('кусок '))
    if failed:
        # Упавший кусок — не «ошибок нет»: молчаливый ноль кандидатов выглядит как чистый текст.
        print(f"⚠ кусков не прочитано: {failed} — список неполон", file=sys.stderr)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

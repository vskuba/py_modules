"""
Разовый круг поиска похожих по фигуре: исходники → Instagram → ранг кандидатов.

Никакого корпуса: паспорт источника считается один раз, каждый источник отдаёт
посты (web_insta), посты качаются в каталог, каждый кадр кандидата получает свой
паспорт и сверяется с источником; пост с несколькими кадрами в ранге усредняется
по своим кадрам. Каталог по умолчанию временный: разовый прогон ничего не
переживает, что бы индексировалось.

Исходники необязательны: без них сверять нечего, и тогда кадры из источников
отдаются списком без оценки (`score: null`, `fields: []`) — судья глаза, а не
страж-цифра. Сверка включается только тем, что пришло с фото-исходниками.
"""
import argparse
import asyncio
import contextlib
import json
import tempfile
from pathlib import Path

from image_.body_.body_ import body_passport
from image_.body_.body_compare import body_compare


async def body_find(photos: list[str], sources: list[str], out: str = '',
                    limit: int = 10, model_name: str = '',
                    concurrency: int = 6) -> dict:
    """Найти в источниках кадры с фигурой, похожей на фигуру исходников.

    Args:
        photos: файлы фото-исходников (по ним строится паспорт источника);
            пусто — сверять нечего, кадры отдаются списком без оценки
        sources: URL источников (профили/посты Instagram — что читает web_insta)
        out: каталог для скачанных кандидатов; пусто — временный (разовый прогон)
        limit: сколько кандидатов вернуть максимум
        model_name: модель зрения с префиксом сервиса; пусто — по умолчанию
        concurrency: сколько кадров качать и читать параллельно

    Returns:
        success: дошли ли до ранга
        passport: сведённый паспорт исходников — пояс «на что похожи»;
            без исходников — пустой, а `items` тогда идут без оценки
        conflicts: поля, где исходные кадры не сошлись
        items: [{pk, score, taken_at, files, fields, caveats}] ранжированно по
            сходству; fields — полевой пояснь кандидата перед человеком
        error: только при success=false
    """
    from web_.web_insta import web_insta_posts, web_insta_download

    src = None
    if photos:
        src = await body_passport(photos, model_name=model_name)
        if not src['success']:
            return {'success': False,
                    'error': f'исходники не прочитаны: {src["error"]}'}

    with contextlib.ExitStack() as stack:
        dir_ = Path(out) if out else Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix='body_')))
        dir_.mkdir(parents=True, exist_ok=True)

        rows: list[dict] = []
        for url in sources:
            got = await web_insta_download(await web_insta_posts(url),
                                           str(dir_), concurrency)
            rows += [r for r in got if 'err' not in r]
        if not rows:
            return {'success': False, 'error': 'источники не отдали ни одного кадра'}

        if src is None:
            # исходников нет — ни паспорта, ни сверки, ни vision-чтения кадров:
            # список в порядке ленты, судья — глаза
            return {'success': True, 'passport': {}, 'conflicts': {},
                    'items': [{'pk': r['pk'] or Path(r['file']).stem,
                               'score': None, 'taken_at': r['taken_at'],
                               'files': [str(dir_ / r['file'])],
                               'fields': [], 'caveats': {}}
                              for r in rows[:limit]],
                    'skipped': []}

        sem = asyncio.Semaphore(concurrency)

        async def read(row: dict) -> dict:
            path = str(dir_ / row['file'])
            async with sem:
                r = await body_passport([path], model_name=model_name)
            cmp = body_compare(src['passport'], r['passport']) if r['success'] else None
            return {'file': path, 'pk': row.get('pk', ''),
                    'taken_at': row.get('taken_at', ''),
                    'cmp': cmp if cmp and cmp['score'] is not None
                    else (cmp or {'score': None}),
                    'error': r.get('error', '')}

        results = list(await asyncio.gather(*[read(r) for r in rows]))
        read_errors = [f"{r['file']}: {r['error']}" for r in results
                       if r['cmp']['score'] is None]

    per_pk: dict[str, list[dict]] = {}
    for r in results:
        if r['cmp']['score'] is not None:
            per_pk.setdefault(r['pk'] or r['file'], []).append(r)
    items = [{'pk': pk or cs[0]['pk'], 'score': round(
                  sum(c['cmp']['score'] for c in cs) / len(cs), 4),
              'taken_at': cs[0]['taken_at'], 'files': [c['file'] for c in cs],
              'fields': cs[0]['cmp']['fields'], 'caveats': cs[0]['cmp']['caveats']}
             for pk, cs in per_pk.items()]
    items.sort(key=lambda i: i['score'], reverse=True)
    return {'success': True, 'passport': src['passport'],
            'conflicts': src['conflicts'], 'items': items[:limit],
            'skipped': read_errors}


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Поиск по источникам: исходники (необязательны) → паспорта '
                    '→ ранг кандидатов; без исходников — лента как есть.')
    p.add_argument('photo', nargs='*', default=[],
                   help='файлы фото-исходников; пусто — список без сверки')
    p.add_argument('--from-url', dest='from_url', action='append', required=True,
                   help='источник (профиль/пост Instagram); повторяемо')
    p.add_argument('--out', default='', help='каталог кандидатов; пусто — временный')
    p.add_argument('--limit', type=int, default=10, help='сколько кандидатов вернуть')
    p.add_argument('--model', default='', help='модель зрения с префиксом сервиса')
    ns = p.parse_args()
    print(json.dumps(asyncio.run(body_find(
        ns.photo, ns.from_url, out=ns.out, limit=ns.limit,
        model_name=ns.model)), ensure_ascii=False, indent=1))

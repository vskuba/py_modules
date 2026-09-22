"""Живой код контейнера: тот ли файл исполняет поднятая панель.

После правки python панель перезапущена — но какой код она подняла? Монтируется
каталог или образ собран с копом правок — вопрос, который каждый раз ловился
руками: `docker compose exec -T <сервис> grep -c 'шаг правки' /app/...`. Эта
функция задаёт его числом совпадений: 0 — контейнер правку не видит, импорт и
перезапуск не помогут, правка live не была.

Совпадения — не строки, а счётчики: ответ обязан быть числом, которое видно
глазом в списке, а не ещё одним трейсом.
"""
from docker_.compose_x import compose_x


def docker_echo(path, must, *, service, app_dir='/app') -> dict:
    """Сколько раз каждый признак правки виден в файле внутри сервиса.

    Args:
        path: путь файла в проекте (тот, что правил).
        must: признаки правки — строки, которые обязаны живеть в файле
            контейнера (`['ge=1', 'steps']`).
        service: имя сервиса compose-стека, который исполняет этот код
            (`persona_uvicorn`, `persona_photo_gen`, …).
        app_dir: корень кода в контейнере (`/app`).

    Returns:
        {'file': path, 'service': service, 'matches': {признак: число}};
        у файла, которого в контейнере нет, число — null.
    """
    inside = f"{app_dir.rstrip('/')}/{path.lstrip('/')}"
    counts = {}
    for m in must:
        r = compose_x(service, ['grep', '-c', '-e', m, inside])
        counts[m] = int(r['output'].strip() or 0) if r['code'] in (0, 1) \
            and r['output'].strip().isdigit() else None
    return {'file': path, 'service': service, 'matches': counts}


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description='Сколько раз каждый признак правки виден в файле внутри '
                    'сервиса: 0 — контейнер правку не видит.')
    ap.add_argument('path', help='путь файла в проекте (тот, что правил)')
    ap.add_argument('--must', action='append', default=[], required=True,
                    metavar='признак', help='строка правки, повторять')
    ap.add_argument('--service', required=True,
                    help='имя сервиса, который исполняет этот код')
    ns = ap.parse_args()
    out = docker_echo(ns.path, ns.must, service=ns.service)
    print(json.dumps(out, ensure_ascii=False))
    # Код возврата — сколько признаков контейнер НЕ увидел (null тоже не увидел).
    raise SystemExit(min(sum(1 for v in out['matches'].values() if not v), 125))

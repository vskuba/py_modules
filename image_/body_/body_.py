"""
Паспорт фигуры: фото-исходники → vision-модель → карточка телосложения (JSON).

Фигуру не меряем пикселями: в кадре нет масштаба, а «пропорции» с наклонённой
камеры расходятся сильнее самой фигуры. Поэтому паспорт — категориальные поля
с закрытым словарём (BODY_CORE_FIELDS), и модель оценивает только то, что
видно; роста в паспорте нет никогда — с одного кадра он всегда выдуман.

Сколько фото: каждое читается отдельно, сводятся в один паспорт модой по полям;
при споре кадров поле становится «не видно», а не правдоподобной выдумкой.
Оговорки кадра (framing/coverage) в сверку как признаки не идут: по ним видно,
почему поле «не видно», и какой кадр в этом виноват.
"""
import argparse
import asyncio
import json
from collections import Counter

# Сверяемые поля и их словари. где порядок значим (ORDERED) — соседние значения
# body_compare сверяет половиной веса, такие ряды стоят по возрастанию признака;
# вне ORDERED порядок значений произволен, сверка только точная.
BODY_CORE_FIELDS = {
    'silhouette': ['песочные часы', 'груша', 'перевернутый треугольник',
                   'прямоугольник', 'яблоко'],  # тип силуэта
    'volume': ['стройное', 'среднее', 'полное'],  # полнота
    'build': ['хрупкое', 'среднее', 'крупное'],  # телосложение
    'waist': ['отсутствует', 'слажен', 'выражен'],
    'shoulders_vs_hips': ['уже', 'равны', 'шире'],
    'legs_vs_torso': ['короче', 'равны', 'длиннее'],
    'muscle': ['гладко', 'рельефно'],
}
BODY_ORDERED = {'volume', 'build', 'waist', 'shoulders_vs_hips',
                'legs_vs_torso', 'muscle'}

# Оговорки кадра, упорядочены от «мешает сверке сильнее» к «мешает меньше»:
# при споре кадров берётся самое мешающее чтение (минимальный индекс).
BODY_FRAME_FIELDS = {
    'framing': ['портрет', 'по пояс', 'по колено', 'весь силуэт'],
    'coverage': ['скрывающая', 'свободная', 'облегающая'],
}

BODY_NOT_VISIBLE = 'не видно'


async def body_passport(paths: list[str], model_name: str = '') -> dict:
    """Составить паспорт фигуры по фото-исходникам (одному или нескольким).

    Каждый кадр читает vision-модель строго JSON-ом по словарям BODY_CORE_FIELDS
    и BODY_FRAME_FIELDS; кадры сводятся полем за полем (мода), при споре поле
    становится «не видно» и попадает в conflicts.

    Args:
        paths: файлы фото-исходников
        model_name: модель с префиксом сервиса (`gx10/qwen-large`); пусто —
            модель по умолчанию из `ai_vision`

    Returns:
        success: удалось ли прочесть хотя бы один кадр
        passport: сведённый паспорт (core-поля + оговорки)
        frames: пофайловая разметка {path, passport | error} — что сказал каждый кадр
        conflicts: поля, где кадры не сошлись ({поле: [значения]})
        error: только при success=false
    """
    from ai.ai_vision import ai_vision_describe  # лениво: LLM-стек нужен только здесь

    async def one(path: str) -> dict:
        # кадр читаем в две попытки: сетевой сбой модели (ReadTimeout и прочий
        # транспорт) чаще лечится вторым запросом, а упрямый остаётся ошибкой
        # одного кадра — прогон из-за него терять нечего, он уйдёт в skipped.
        for retry in (False, True):
            try:
                raw = await ai_vision_describe(
                    open(path, 'rb').read(), _body_prompt(), model_name=model_name)
                return {'path': str(path), 'passport': _body_json(raw)}
            except Exception as err:
                if retry:
                    return {'path': str(path),
                            'error': f'{type(err).__name__}: {err}'}
                await asyncio.sleep(2)

    frames = list(await asyncio.gather(*[one(p) for p in paths]))
    read = [f['passport'] for f in frames if 'passport' in f]
    if not read:
        return {'success': False, 'error': 'ни один кадр не прочитан: '
                + '; '.join(f"{f['path']}: {f['error']}" for f in frames)}
    merged, conflicts = _body_merge(read)
    return {'success': True, 'passport': merged, 'frames': frames,
            'conflicts': conflicts}


def body_passport_wait(paths: list[str], model_name: str = '') -> dict:
    """Синхронный вход в `body_passport` — для CLI и скриптов без своего цикла."""
    return asyncio.run(body_passport(paths, model_name))


def _body_prompt() -> str:
    """Задание модели: поля, закрытые словари, «не видно» вместо догадки."""
    lines = '; '.join(f'{f}: {" | ".join(vals + [BODY_NOT_VISIBLE])}'
                      for f, vals in {**BODY_CORE_FIELDS, **BODY_FRAME_FIELDS}.items())
    return ('Оцени телосложение человека на фото. Ответь ТОЛЬКО JSON-объектом '
            'без пояснений, полями: ' + lines + '. '
            f'Пиши «{BODY_NOT_VISIBLE}», когда поле по кадру не определить '
            '(портрет, одежда, ракурс). Ничего не додумывай: рост по одному '
            'кадру не оценивай вовсе — его в списке полей нет.')


def _body_json(text: str) -> dict:
    """Вынуть паспорт из ответа модели: снять огранку ```json, разобрать объект."""
    body = text.strip().partition('{')[2].rpartition('}')[0]
    if not body:
        raise ValueError(f'модель не ответила JSON: {text[:120]!r}')
    try:
        data = json.loads('{' + body + '}')
    except json.JSONDecodeError as err:
        raise ValueError(f'JSON ответа не разобрался: {err}') from err
    known = set(BODY_CORE_FIELDS) | set(BODY_FRAME_FIELDS)
    return {k: v for k, v in data.items() if k in known}


def _body_merge(profiles: list[dict]) -> tuple[dict, dict]:
    """Свести пофайловые паспорта: мода по каждому полю; core в споре — не видно.

    Оговорки не мотятся, а берутся худшим чтением кадров: один скрывающий кадр
    обесценивает три облегающих — отсюда min по индексу ряда оговорки.
    """
    merged, conflicts = {}, {}
    for field, order in {**BODY_CORE_FIELDS, **BODY_FRAME_FIELDS}.items():
        vals = [p[field] for p in profiles if p.get(field)]
        if not vals:
            continue
        if len(set(vals)) > 1:
            conflicts[field] = sorted(set(vals))
        if len(set(vals)) == 1:
            merged[field] = vals[0]
        elif field in BODY_FRAME_FIELDS:
            # худшее из чтений кадра: «не видно» хуже любого чтения ряда — кадр,
            # который ничего не показывает, обесценивает показания соседних.
            merged[field] = (BODY_NOT_VISIBLE if BODY_NOT_VISIBLE in vals
                             else min(vals, key=order.index))
        else:
            top = Counter(vals).most_common(2)
            merged[field] = (top[0][0] if len(top) == 1 or top[0][1] > top[1][1]
                             else BODY_NOT_VISIBLE)
    return merged, conflicts


if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Паспорт фигуры по фото: кадры → сведённый JSON-паспорт.')
    p.add_argument('path', nargs='+', help='файлы фото-исходников')
    p.add_argument('--model', default='', help='модель с префиксом сервиса, '
                                               'например gx10/qwen-large')
    ns = p.parse_args()
    print(json.dumps(body_passport_wait(ns.path, ns.model),
                     ensure_ascii=False, indent=1))

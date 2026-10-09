"""Прогон случая: страница → кадр, замеры, вердикты. Результат — словарём.

Здесь кончается общее и начинается проектное. Прогон **ничего не знает** ни про
базу, ни про страницу результатов: он отдаёт словарь на случай × ширину, а куда
его положить — дело проекта. Поэтому каркас и едет во все проекты целиком, а
таблица у каждого своя.

```python
for width in case['widths']:
    result = tests_e2e_run_case(case, width, folder='/app/data/e2e/<run>', base=...)
    await e2e_row_write(run_uuid, result)
```

⚠⚠ Функция **синхронная**, и зовут её из async-кода через `asyncio.to_thread`.
Внутри три запуска Chrome подряд через `subprocess`, и каждый блокирует поток на
секунды: позови её прямо из кронджоба — и встанет весь его цикл событий.

⚠ Страница панели живёт за входом, поэтому прогон идёт не по её адресу, а по
копии (`uvicorn_mirror`): копия несёт куку входа и проксирует `/api/` на панель.
Без этого вместо страницы снимался бы экран входа — и кадр выглядел бы честно.
"""

import os
import time

from uvicorn_.uvicorn_mirror import uvicorn_mirror

from tests_e2e.tests_e2e_case import tests_e2e_case_size
from tests_e2e.tests_e2e_probe import tests_e2e_probe
from tests_e2e.tests_e2e_rule import tests_e2e_rule_check, tests_e2e_rule_check_value
from tests_e2e.tests_e2e_shot import tests_e2e_shot_marked


def tests_e2e_run_case(case: dict, width: int, folder: str, base: str = '',
                       names: tuple = (), chrome: str = '') -> dict:
    """Прогнать случай в одной ширине.

    Args:
        case: случай (`tests_e2e_case_load`).
        width: ширина вьюпорта.
        folder: куда положить кадры.
        base: адрес панели; пусто — `uvicorn_panel_base()`.
        names: (имя чистого файла, имя размеченного); пусто — по имени случая.
        chrome: путь к бинарю браузера.

    Returns:
        dict: `name`, `description`, `page`, `width`, `status`, `areas`,
        `console`, `shot_path`, `shot_plain_path`, `error`, `duration_ms`.
        `status` — `ok`, `fail` (нашли дефект) или `error` (прогон не состоялся).

    ⚠ Исключение наружу не выпускается: сорвавшийся случай — это `error`-строка
    в отчёте, а не упавший прогон. Иначе один мёртвый случай уносил бы с собой
    все остальные, и страница результатов осталась бы вчерашней.
    """
    began = time.time()
    size = tests_e2e_case_size(width)
    plain_name, marked_name = names or (f'{width}_plain.png', f'{width}_marked.png')
    plain = os.path.join(folder, plain_name)
    marked = os.path.join(folder, marked_name)

    head = {'name': case['name'], 'description': case['description'],
            'page': case['page'], 'width': width}

    try:
        # ⚠⚠ Кадр и замер — ОДИН прогон браузера: см. шапку `tests_e2e_probe`.
        # Разнеси их — и на кадре окажется состояние до шагов случая.
        with uvicorn_mirror(case['page'], base=base) as url:
            seen = tests_e2e_probe(url, case, size, shot=plain, chrome=chrome)
    except Exception as err:
        return {**head, 'status': 'error', 'areas': [], 'console': [],
                'error': f'{type(err).__name__}: {err}',
                'duration_ms': int((time.time() - began) * 1000)}

    areas = tests_e2e_run_verdicts(case, seen)
    tests_e2e_shot_marked(plain, marked, areas)

    failed = sum(1 for area in areas if area['status'] != 'ok')
    return {
        **head,
        'status': 'fail' if failed else 'ok',
        'areas': areas,
        'console': seen.get('console') or [],
        'shot_path': marked,
        'shot_plain_path': plain,
        'duration_ms': int((time.time() - began) * 1000),
    }


def tests_e2e_run_verdicts(case: dict, seen: dict) -> list:
    """Замеры зонда → список областей с вердиктами.

    Проверки содержимого (`checks`) приходят сюда же, отдельной областью без
    координат: на странице результатов они читаются тем же списком, что и
    геометрия, — у человека один вопрос «что не сошлось», а не два.
    """
    viewport = int(seen.get('viewport') or 0)
    out = []

    for area in case.get('areas') or []:
        got = (seen.get('areas') or {}).get(area['selector'])
        rules = tests_e2e_rule_check(area['rules'], got, viewport)
        out.append({
            'name': area['name'],
            'selector': area['selector'],
            'rect': [got['x'], got['y'], got['w'], got['h']] if got else None,
            'rules': rules,
            'status': 'ok' if all(one['status'] == 'ok' for one in rules) else 'fail',
        })

    checks = case.get('checks') or []
    if checks:
        verdicts = [tests_e2e_rule_check_value(check, (seen.get('checks') or {}).get(check['name']))
                    for check in checks]
        out.append({
            'name': 'Содержимое',
            'selector': '',
            'rect': None,
            'rules': verdicts,
            'status': 'ok' if all(one['status'] == 'ok' for one in verdicts) else 'fail',
        })

    # Шаг, который не вышел, — это не провал вёрстки, а непроверенная страница:
    # не нажалась вкладка — мерили не то, что просили.
    broken = seen.get('steps') or []
    if broken:
        out.append({
            'name': 'Шаги до замера',
            'selector': '',
            'rect': None,
            'rules': [{'name': one.get('step', 'шаг'), 'status': 'fail',
                       'detail': one.get('error', 'не выполнился')} for one in broken],
            'status': 'fail',
        })

    return out

"""Правила областей: замер → вердикт с числом.

Правило отвечает не «да/нет», а **числом**: «правый край 1147 при вьюпорте
360». Без числа вердикт нечем проверить и не с чем сравнить в следующем
прогоне, а «не вылезла» без числа неотличимо от «не проверяли».

Набор открытый: новое правило — функция в `TESTS_E2E_RULE_ALL` и строка в
`rules:` случая. Имена русские, потому что их читает человек на странице
результатов, а не только тот, кто пишет YAML.

⚠ Мёртвый селектор даёт `fail` со своим текстом, а не ноль. Ноль из мёртвого
селектора неотличим от сломанного элемента (`frontend_check_rules.md`, § 10):
устаревший `id` иначе выдаёт «элемент не работает» на рабочий элемент.
"""


def tests_e2e_rule_check(names: list, got: dict, viewport: int) -> list:
    """Проверить область всеми её правилами.

    Args:
        names: имена правил из случая.
        got: замер области из зонда (`None` — селектор не нашёл узла).
        viewport: ширина вьюпорта этого прогона.

    Returns:
        list: по словарю на правило — `name`, `status` (`ok`/`fail`), `detail`.
    """
    out = []
    for name in names:
        if got is None:
            out.append({'name': name, 'status': 'fail',
                        'detail': 'элемент не отрисовался: селектор не нашёл узла'})
            continue

        rule = TESTS_E2E_RULE_ALL.get(name)
        if not rule:
            out.append({'name': name, 'status': 'fail',
                        'detail': 'правила с таким именем нет'})
            continue

        out.append({'name': name, **rule(got, viewport)})
    return out


def tests_e2e_rule_visible(got: dict, viewport: int) -> dict:
    """Элемент занимает место на экране."""
    ok = got['w'] > 0 and got['h'] > 0
    return {'status': 'ok' if ok else 'fail',
            'detail': f"{got['w']}×{got['h']} px"}


def tests_e2e_rule_inside(got: dict, viewport: int) -> dict:
    """Правый край элемента не выходит за вьюпорт.

    ⚠ Допуск в 1 px намеренный: `getBoundingClientRect` отдаёт дробные
    координаты, и округление само по себе давало бы ложный провал.
    """
    right = got['x'] + got['w']
    return {'status': 'ok' if right <= viewport + 1 else 'fail',
            'detail': f'правый край {right} при вьюпорте {viewport}'}


def tests_e2e_rule_whole(got: dict, viewport: int) -> dict:
    """Содержимое влезло в свой блок — текст не обрезан."""
    ok = not got['clipped']
    return {'status': 'ok' if ok else 'fail',
            'detail': ('содержимое влезло' if ok
                       else f"содержимое шире блока на {got['over']} px")}


def tests_e2e_rule_filled(got: dict, viewport: int) -> dict:
    """В элементе есть текст: пустой контейнер на месте виджета глазами не
    отличить от «данные ещё грузятся»."""
    return {'status': 'ok' if got['text'] > 0 else 'fail',
            'detail': f"знаков текста {got['text']}"}


def tests_e2e_rule_check_value(check: dict, seen: dict) -> dict:
    """Проверка содержимого: ожидаемое число против полученного.

    Args:
        check: `name`, `expect`, `js` из случая.
        seen: что вернул зонд по этому имени — `{'value': n}` или `{'error': …}`.

    Returns:
        dict: `name`, `status`, `detail` — как у правила области.
    """
    if not seen:
        return {'name': check['name'], 'status': 'fail',
                'detail': 'зонд не дал ответа на эту проверку'}
    if 'error' in seen:
        return {'name': check['name'], 'status': 'fail',
                'detail': f"проверка упала: {seen['error']}"}

    value = seen.get('value')
    expect = check.get('expect', 0)
    ok = value == expect
    return {'name': check['name'], 'status': 'ok' if ok else 'fail',
            'detail': f'ожидали {expect}, получили {value}'}


TESTS_E2E_RULE_ALL = {
    'видна': tests_e2e_rule_visible,
    'не_вылезла': tests_e2e_rule_inside,
    'не_обрезана': tests_e2e_rule_whole,
    'непустая': tests_e2e_rule_filled,
}

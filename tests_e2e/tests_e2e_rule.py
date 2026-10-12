"""Правила областей: замер → вердикт с числом.

Правило отвечает не «да/нет», а **числом**: «правый край 1147 при вьюпорте
360». Без числа вердикт нечем проверить и не с чем сравнить в следующем
прогоне, а «не вылезла» без числа неотличимо от «не проверяли».

Набор открытый: новое правило — функция в `TESTS_E2E_RULE_ALL` и строка в
`rules:` случая.

⚠ **Имя правила = суффикс его функции**: `inside` → `tests_e2e_rule_inside`.
Связь не формальная, а рабочая: в YAML видно только имя, и от него до кода,
который считает вердикт, должен быть один шаг без поиска по словарю.

⚠⚠ Имена были русскими (`видна`, `не_вылезла`) — соображение было, что их
читает человек на странице результатов. Соображение неверное: на странице
правило показывается вместе со своим числом («правый край 1147 при вьюпорте
360»), и понятно оно оттуда, а не из имени. Платили за это тем, что ключи
словаря не проходили проверку имён (`tool_typo --cyrillic`) и в YAML шла
кириллица вперемешку с селекторами.

⚠⚠ Смена ломает старые случаи: незнакомое имя даёт `fail` со своим текстом, а
не молчание. Поэтому **указатель `py_modules` и YAML проекта едут одним
коммитом** — иначе прогон после сдвига submodule покраснеет целиком. Уже
записанные прогоны при этом не трогаются: там имена лежат копией, и вчерашний
отчёт обязан читаться вчерашними словами.

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
            # ⚠ Перечисляем известные прямо в вердикте. В это место упирается
            # случай, отставший от переименования правил, и «правила с таким
            # именем нет» заставляло бы лезть в исходник за списком — при том
            # что ответ короткий и всегда один и тот же.
            known = ', '.join(sorted(TESTS_E2E_RULE_ALL))
            out.append({'name': name, 'status': 'fail',
                        'detail': f'правила с таким именем нет; есть: {known}'})
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


# ⚠ Ключ = суффикс функции (см. шапку). Проверить соответствие дешевле, чем
# искать: `visible` → `tests_e2e_rule_visible` в этом же файле.
TESTS_E2E_RULE_ALL = {
    'visible': tests_e2e_rule_visible,
    'inside': tests_e2e_rule_inside,
    'whole': tests_e2e_rule_whole,
    'filled': tests_e2e_rule_filled,
}

"""Замазывание секретов во всём, что уезжает из прогона наружу.

Через это проходит **каждая** строка, которую прогон отдаёт вызывающему: трасса
шага, текст ошибки, собранные данные. Причина одна и она не про аккуратность:
Playwright цитирует введённое в собственных сообщениях (`fill("hunter2")` в тексте
таймаута), а страница показывает то, что мы в неё же и ввели — логин в шапке
кабинета, адрес почты в поле профиля. Без замазывания пароль от чужого сайта уехал
бы в базу через текст ошибки и в ответ API через собранные записи, мимо всей той
защиты, ради которой значения шифруются (`browser_secret`).
"""

BROWSER_SCENARIO_MASK = '***'

# Секрет короче этого не замазывается: затирать в тексте каждую «1» или «ok»
# вреднее, чем оставить — от такой маски журнал перестаёт читаться.
BROWSER_SCENARIO_MASK_MIN_LEN = 4


def browser_scenario_mask(text: str, secrets: dict) -> str:
    """Замазывает значения секретов в тексте — журнал и ошибки идут через это."""
    if not text:
        return text

    for value in secrets.values():
        value = str(value)
        if len(value) >= BROWSER_SCENARIO_MASK_MIN_LEN:
            text = text.replace(value, BROWSER_SCENARIO_MASK)

    return text


def browser_scenario_mask_data(data, secrets: dict):
    """
    То же замазывание, но по дереву собранных записей.

    Нужно потому, что данные приезжают не строкой, а списком словарей (`extract_rows`,
    `net_dump`, `console_dump`), и обход вглубь здесь не перестраховка: в теле запроса
    входа лежит пароль, а в записи консоли — то, что страница о нём напечатала.
    Плоское замазывание пропустило бы и то, и другое.
    """
    if isinstance(data, str):
        return browser_scenario_mask(data, secrets)
    if isinstance(data, list):
        return [browser_scenario_mask_data(item, secrets) for item in data]
    if isinstance(data, dict):
        return {key: browser_scenario_mask_data(value, secrets) for key, value in data.items()}

    return data

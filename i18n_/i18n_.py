"""
Транслитерация и мултиязычные значения текста.

`i18n_translit` — таблица постановления КМУ № 55 от 27.01.2010 «Про
впорядкування транслітерації українського алфавіту латиницею»
(zakon.rada.gov.ua/laws/show/55-2010-п): обязательна для собственных имён
в документах, только ASCII без диакритики. Резолверы переводов — про
хранилище: значения лежат JSON-словарями по кодам языков.
"""
import argparse
import json
import re

# Значение-перевод: JSON-словарь, где ВСЕ ключи — коды языков ('ru', 'en', 'uk'...).
# Обычные JSON-значения (фильтры {"user_id": ...}) под это условие не попадают.
_LANG_CODE = re.compile(r'^[a-z]{2}(-[a-zA-Z]{2})?$')


def i18n_text_resolve(value, language: str):
    """
    Резолвит мультиязычное значение: если value — строка с JSON-словарём переводов
    {"ru": "...", "en": "..."}, возвращает перевод для language
    (fallback: первый язык словаря). Любое другое значение возвращается как есть.
    """
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not (text.startswith('{') and text.endswith('}')):
        return value

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return value

    if not isinstance(data, dict) or not data:
        return value

    if not all(isinstance(k, str) and _LANG_CODE.match(k) for k in data.keys()):
        return value

    resolved = data.get(language)
    if resolved is None:
        resolved = next(iter(data.values()))
    return resolved


def i18n_languages_parse(languages: str) -> list[str]:
    """'ru, en' -> ['ru', 'en']; пустая строка -> ['ru']."""
    result = [x.strip().lower() for x in str(languages or '').split(',') if x.strip()]
    return result or ['ru']


# Таблица КМУ № 55: буква -> латиница. Многобуквенные соответствия
# (ж, х, ц, ч, ш, щ) — фиксированные диграфы, регистр правится у первой.
I18N_LATIN = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
}

# Є/Ї/Й/Ю/Я: на початку слова и в інших позиціях — разные записи
# (Єнакієве — Yenakiieve, Гаєвич — Haievych).
I18N_INITIAL = {'є': 'ye', 'ї': 'yi', 'й': 'y', 'ю': 'yu', 'я': 'ya'}
I18N_JOINED = {'є': 'ie', 'ї': 'i', 'й': 'i', 'ю': 'iu', 'я': 'ia'}

# Знак м'якості и апостроф латиницей не отбрасываются — но и слово не разрывают:
# после них идёт не-начальный облик (Знам'янка — Znamianka). Три вида апострофа:
# типографский, ASCII и модифицированный.
I18N_DROPPED = 'ь\'’ʼ'


def i18n_translit(text: str) -> str:
    """
    Транслитерация украинского текста латиницей по таблице КМУ № 55 (2010).

    Буквы — одна к одной; Є/Ї/Й/Ю/Я имеют два облика: початковый (Ye, Yi, Y,
    Yu, Ya) и присоединённый (ie, i, i, iu, ia). Слово — последовательность
    букв: любой нетранслитерируемый символ начинает новое слово, а мягкий
    знак и апостроф — нет. Латиница, цифры и пунктуация проходят как есть,
    верхний регистр сохраняется (МІСТО — MISTO).

    Args:
        text: произвольный текст; украинские буквы переводятся, остальные — зеркало.

    Returns:
        Строка только из ASCII там, где был кириллический текст.
    """
    out = []
    in_word = False
    for ch in text:
        low = ch.casefold()
        if low in I18N_DROPPED:
            continue
        if low in I18N_LATIN:
            form = I18N_LATIN[low]
        elif not in_word and low in I18N_INITIAL:
            form = I18N_INITIAL[low]
        elif low in I18N_JOINED:
            form = I18N_JOINED[low]
        else:
            out.append(ch)
            in_word = False
            continue
        out.append(form[:1].upper() + form[1:] if ch.isupper() else form)
        in_word = True
    return ''.join(out)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Транслитерация украинского текста латиницей (таблица КМУ № 55).',
        epilog='i18n Гетьман Сагайдачний')
    parser.add_argument('text', nargs='+', help='слова украинского текста')
    print(i18n_translit(' '.join(parser.parse_args().text)))
